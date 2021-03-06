#!/usr/bin/env python
# -*- coding: utf-8 -*- 

#
# Copyright 2015, 2016, 2017 Guenter Bartsch
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# AI-Prolog (based on Zamia-Prolog)
#
# grammar is exactly the same, this one has just a few predicates that are 
# handled in a special way to make writing down NLP training data easier:
#
# * train/1
# * train_priority/1
# * train_prefix/1
# * test/2
# * train_ner/4
#

import os
import sys
import logging
import codecs
import re

from copy                import copy
from io                  import StringIO

from zamiaprolog.parser  import PrologParser, SYM_EOF
from zamiaprolog.logic   import *
from zamiaprolog.errors  import *
from zamiaprolog.runtime import PrologRuntime
from nltools.tokenizer   import tokenize

class AIPrologParser(PrologParser):

    def __init__(self, db):
        self.rt         = PrologRuntime(db)
        super(AIPrologParser, self).__init__(db)
    
    def compile_file (self, filename, module_name, clear_module=False, run_trace=False):

        self.rt.set_trace(run_trace)

        # quick source line count for progress output below

        self.linecnt = 1
        with codecs.open(filename, encoding='utf-8', errors='ignore', mode='r') as f:
            while f.readline():
                self.linecnt += 1
        logging.info("%s: %d lines." % (filename, self.linecnt))

        # remove old predicates of this module from db
        if clear_module:
            self.clear_module (module_name, db)

        # (re-) init

        self.ds             = []
        self.ts             = []
        self.ner            = {}
        self.named_macros   = {}
        self.lang           = 'en'
        self.train_prio     = 0
        self.train_prefixes = []

        # actual parsing starts here

        with codecs.open(filename, encoding='utf-8', errors='ignore', mode='r') as f:
            self.start(f, filename, module_name=module_name, linecnt=self.linecnt)

            while self.cur_sym != SYM_EOF:
                clauses = self.clause()

                for clause in clauses:
                    logging.debug(u"%7d / %7d (%3d%%) > %s" % (self.cur_line, self.linecnt, self.cur_line * 100 / self.linecnt, unicode(clause)))

                    if clause.head.name == 'train':
                        self.extract_training_data (clause)
                    elif clause.head.name == 'train_priority':
                        self.extract_training_priority (clause)
                    elif clause.head.name == 'train_prefix':
                        self.extract_training_prefixes (clause)
                    elif clause.head.name == 'test':
                        self.extract_test_data (clause)
                    elif clause.head.name == 'train_ner':
                        self.extract_ner_training (clause)
                        
                    else:
                        self.db.store (module_name, clause)

                if self.comment_pred:

                    self.db.store_doc (module_name, self.comment_pred, self.comment)

                    self.comment_pred = None
                    self.comment = ''

        self.db.commit()

        logging.info("Compilation succeeded.")

        return self.ds, self.ts, self.ner

    ###############################################
    #
    # training data extraction 
    #
    ###############################################

    def fetch_named_macro (self, lang, name):

        if not lang in self.named_macros:
            self.named_macros[lang] = {}

        if name in self.named_macros[lang]:
            return self.named_macros[lang][name]

        # extract variable binding from prolog macro, if any

        macros = self.db.lookup ('macro', arity=-1)

        for macro in macros:
            if len(macro.head.args)<2:
                continue
            if not isinstance(macro.head.args[0], Predicate) or macro.head.args[0].name != lang:
                continue
            if not isinstance(macro.head.args[1], Predicate) or macro.head.args[1].name != name:
                continue

            args = [lang, name]
            for a in macro.head.args[2:]:
                if not isinstance(a, Variable):
                    self.report_error (u'invalid macro %s encountered.' % unicode(macro))
                args.append(a.name)

            # FIXME: debug only
            # if name == 'cities':
            #     import pdb; pdb.set_trace()

            solutions = self.rt.search_predicate('macro', args)

            if solutions:
                self.named_macros[lang][name] = solutions
                return solutions

            break

        return None

    def _expand_macros (self, txt):

        logging.debug(u"expand macros  : %s" % txt)

        implicit_macros = {}

        txt2 = ''

        i = 0
        while i<len(txt):

            if txt[i] == '(':

                j = txt[i+1:].find(')')
                if j<0:
                    self.report_error (') missing')
                j += i

                # extract macro

                macro_s = txt[i+1:j+1]

                # print "macro_s: %s" % macro_s

                macro_name = 'MACRO_%d' % len(implicit_macros)

                implicit_macros[macro_name] = []
                for s in macro_s.split('|'):
                    sub_parts = tokenize(s, lang=self.lang, keep_punctuation=False)
                    implicit_macros[macro_name].append({'W': sub_parts})

                txt2 += '{' + macro_name + ':W}'

                i = j+2
            else:

                txt2 += txt[i]
                i+=1

        logging.debug ( "implicit macros: %s" % repr(implicit_macros) )
        logging.debug ( "txt2           : %s" % txt2 )

        parts = []
        for p1 in txt2.split('{'):
            for p2 in p1.split('}'):
                parts.append(p2)

        done = []

        todo = [ (parts, 0, [], {}, {}) ]

        # import pdb; pdb.set_trace()
        while len(todo)>0:

            parts1, cnt, r, mpos, macro_rs = todo.pop()

            if cnt >= len(parts1):
                done.append((r, mpos))
                continue

            p1 = parts1[cnt]

            if cnt % 2 == 1:
                
                sub_parts = p1.split(':')

                if len(sub_parts) != 2:
                    self.report_error ('syntax error in macro call %s' % repr(p1))

                name = sub_parts[0]

                if name == 'empty':
                    todo.append((parts, cnt+1, copy(r), mpos, copy(macro_rs)))
                else:

                    vn    = sub_parts[1]

                    if name in macro_rs:
                        macro = [ macro_rs[name] ]
                    else:
                        macro = self.fetch_named_macro(self.lang, name)
                        if not macro:
                            macro = implicit_macros.get(name, None)
                        if not macro:
                            self.report_error ('unknown macro "%s"[%s] called' % (name, self.lang))

                    for r3 in macro:
                        r1        = copy(r)
                        mpos1     = copy(mpos)
                        macro_rs1 = copy(macro_rs)

                        macro_rs1[name] = r3

                        # take care of multiple invocactions of the same macro
        
                        mpnn = 0
                        while True:
                            mpn = '%s_%d_start' % (name, mpnn)
                            if not mpn in mpos1:
                                break
                            mpnn += 1

                        mpos1['%s_%d_start' % (name, mpnn)] = len(r1)
                        s3 = r3[vn]
                        if isinstance (s3, StringLiteral):
                            s3 = tokenize (s3.s, lang=self.lang)
                            r3[vn] = s3
                        r1.extend(r3[vn])
                        mpos1['%s_%d_end' % (name, mpnn)]   = len(r1)

                        for vn3 in r3:
                            mpos1['%s_%d_%s' % (name, mpnn, vn3.lower())] = r3[vn3]

                        todo.append((parts, cnt+1, r1, mpos1, macro_rs1))
                        
                        # if name == 'home_locations':
                        #     import pdb; pdb.set_trace()

            else:

                sub_parts = tokenize(p1, lang=self.lang, keep_punctuation=False)

                r  = copy(r)
                r.extend(sub_parts)

                todo.append((parts, cnt+1, r, mpos, macro_rs))

        return done

    def _token_positions (self, a, mpos):

        if isinstance (a, Predicate):

            if a.name == 'tstart':

                if len(a.args) == 1:
                    occ = 0
                    tname = a.args[0]
                elif len(a.args) == 2:
                    occ   = int(a.args[1].f)
                    tname = a.args[0]
                else:
                    self.report_error ('tstart: one or two args expected, found "%s" instead' % unicode(a))

                k = '%s_%d_start' % (tname, occ)
                if not k in mpos:
                    self.report_error ('tstart: could not determine "%s"' % unicode(a))

                return NumberLiteral(mpos[k])

            elif a.name == 'tend':

                if len(a.args) == 1:
                    occ = 0
                    tname = a.args[0]
                elif len(a.args) == 2:
                    occ   = int(a.args[1].f)
                    tname = a.args[0]
                else:
                    self.report_error ('tend: one or two args expected, found "%s" instead' % unicode(a))

                k = '%s_%d_end' % (tname, occ)
                if not k in mpos:
                    self.report_error ('tend: could not determine "%s"' % unicode(a))

                return NumberLiteral(mpos[k])

            elif a.name == 'mvar':

                # import pdb; pdb.set_trace()
                if len(a.args) == 2:
                    tname = a.args[0]
                    vname = a.args[1]
                    occ = 0
                elif len(a.args) == 3:
                    tname = a.args[0]
                    vname = a.args[1]
                    occ   = int(a.args[2].f)
                else:
                    self.report_error ('mvar: one or two args expected, found "%s" instead' % unicode(a))

                k = '%s_%d_%s' % (tname, occ, vname)
                if not k in mpos:
                    self.report_error ('mvar: could not determine "%s"' % unicode(a))

                return mpos[k]

            else:

                margs = []
                for a2 in a.args:
                    margs.append(self._token_positions (a2, mpos))

                return Predicate (a.name, margs)

        elif isinstance (a, ListLiteral):

            l2 = []
            for a2 in a.l:
                l2.append(self._token_positions (a2, mpos))

            return ListLiteral (l2)

        return a

    def _generate_training_code (self, and_args, mpos, and_mode=True):

        # import pdb; pdb.set_trace()

        code = []

        for a in and_args:

            if isinstance (a, StringLiteral):

                # code.append(Predicate('r_bor', [ Variable('C') ]))
                # code.append(u'r_bor(C)')

                parts = []
                for p1 in a.s.split('{'):
                    for p2 in p1.split('}'):
                        parts.append(p2)

                if not and_mode:
                    code.append(u'and(')

                cnt = 0
                for part in parts:

                    if cnt % 2 == 1:

                        subparts = part.split(',')

                        if len(subparts)!=2:
                            self.report_error ('variable string "%s" not recognized .' % repr(part))

                        var_s = subparts[0]
                        fmt_s = subparts[1]

                        code.append(unicode(Predicate('r_sayv', [ Variable('C'), Variable(var_s), Predicate(fmt_s) ])))

                    else:

                        for t in tokenize(part, lang=self.lang, keep_punctuation=True):
                            code.append(unicode(Predicate('r_say', [ Variable('C'), StringLiteral(t)])))
                    cnt += 1

                if not and_mode:
                    code.append(u')')

            elif isinstance (a, Predicate):

                if a.name == 'or':

                    code.append(u'or(')

                    code.extend(self._generate_training_code (a.args, mpos, and_mode=False))

                    code.append(u')')

                elif a.name =='and':

                    code.append(u'and(')

                    code.extend(self._generate_training_code (a.args, mpos))

                    code.append(u')')

                else:

                    code.append(unicode(self._token_positions(a, mpos)))

            else:
                code.append(unicode(self._token_positions(a, mpos)))

        return code

    def _extract_context(self, a):
        if len(a.args) != 2:
            self.report_error('context: 2 args expected.')

        a0 = a.args[0]
        if isinstance(a0, StringLiteral):
            a0 = a0.s
        elif isinstance(a0, Predicate):
            a0 = a0.name
        else:
            self.report_error('context: string or constant expected, %s found instead.' % repr(a0))
        a1 = a.args[1]
        if isinstance(a1, StringLiteral):
            a1 = a1.s
        elif isinstance(a1, Predicate):
            a1 = a1.name
        else:
            self.report_error('context: string or constant expected, %s found instead.' % repr(a1))

        return [a0, a1]

    def extract_training_data (self, clause):

        if len(clause.head.args) != 1:
            self.report_error ('train: single language argument expected')
        self.lang = clause.head.args[0].name

        if not clause.body or clause.body.name != 'and':
            self.report_error ('train: flat (and) body expected')

        # filter out contexts, input line

        context_sets = []
        inps         = []
        code         = []
        for a in clause.body.args:

            if not inps:
                if isinstance(a, StringLiteral):
                    inps.append(a.s)
                    continue

                if not isinstance (a, Predicate):
                    self.report_error('input string(s) or context specifier expected.')
                    continue
                    
                if a.name == 'context':
                    if len(context_sets)>1:
                        self.report_error('more than one context specifier found.')
                    elif not context_sets:
                        context_sets.append([])

                    context_sets[0].append(self._extract_context(a))

                elif a.name == 'or':
                    if isinstance (a.args[0], StringLiteral):
                        for a1 in a.args:
                            if not isinstance (a1, StringLiteral):
                                self.report_error('input string(s) expected.')
                            inps.append(a1.s) 
  
                    elif isinstance (a.args[0], Predicate):
                        for a1 in a.args:
                            if not isinstance (a1, Predicate):
                                self.report_error('context specifier expected.')

                            if a1.name == 'context':
                                context_sets.append([self._extract_context(a1)])
                            elif a1.name == 'and':
                                context_sets.append([])
                                for a2 in a1.args:
                                    if not isinstance (a2, Predicate) or a2.name != 'context':
                                        self.report_error('context specifier expected.')
                                    context_sets[len(context_sets)-1].append(self._extract_context(a2))
                            elif a1.name == 'true':
                                context_sets.append([])
                            else:
                                self.report_error('context specifier expected.')
                    else:
                        self.report_error('input string(s) or context specifier expected.')

                else:
                    self.report_error('input string(s) or context specifier expected.')

                continue

            code.append(a)

        if not inps:
            self.report_error ('train: no input string(s).')

        if not context_sets:
            context_sets.append([])

        if len(code)==0:
            self.report_error ('train: no code.')

        prefixes = self.train_prefixes if self.train_prefixes else [u'']

        for prefix in prefixes:

            for inp in inps:

                pinp = prefix + inp

                for d, mpos in self._expand_macros(pinp):

                    r = self._generate_training_code (code, mpos)

                    logging.debug( '%s -> %s' % (repr(d), repr(r)))

                    for contexts in context_sets:

                        self.ds.append((self.lang, contexts, d, r, clause.location.fn, clause.location.line, clause.location.col, self.train_prio))

                        if len(self.ds) % 100 == 0:
                            logging.info ('%6d training samples extracted so far...' % len(self.ds))


    def extract_training_priority (self, clause):

        # import pdb; pdb.set_trace()

        if len(clause.head.args) != 1:
            self.report_error ('train_priority: single priority argument expected')

        self.train_prio = self.rt.prolog_get_int(clause.head.args[0], {}, clause.location)

    def extract_training_prefixes (self, clause):

        if len(clause.head.args) != 1:
            self.report_error ('train_prefixes: single prefix argument expected')

        if not clause.body:
            prefix = self.rt.prolog_get_string(clause.head.args[0], {}, clause.location)
            self.train_prefixes.append(prefix)
            return

        # import pdb; pdb.set_trace()

        v = self.rt.prolog_get_variable(clause.head.args[0], {}, clause.location)

        solutions = self.rt.search(clause, env={})
        for s in solutions:
            prefix = s[v].s
            self.train_prefixes.append(prefix)

    def extract_test_data (self, clause):

        if len(clause.head.args) != 2:
            self.report_error ('test: 2 arguments (lang, test_name) expected')

        self.lang = clause.head.args[0].name
        test_name = clause.head.args[1].name

        if not clause.body or clause.body.name != 'and':
            self.report_error ('test: flat (and) body expected')

        prep    = []
        rounds  = []

        inp     = None
        resp    = None
        actions = []
        cnt     = 0

        for a in clause.body.args:

            # import pdb; pdb.set_trace()
            if isinstance (a, StringLiteral):

                if cnt % 2 == 0:
                    if inp:
                        rounds.append ((inp, resp, actions))
                    inp     = tokenize(a.s, lang=self.lang, keep_punctuation = False)
                    resp    = None
                    actions = []

                else:
                    resp    = tokenize(a.s, lang=self.lang, keep_punctuation = False)

                cnt += 1
            else:

                if not inp:
                    prep.append(a)
                else:
                    if not isinstance(a, Predicate) or a.name != 'action':
                        self.report_error('only action predicates allowed here.')
                    actions.append(list(map (lambda x: unicode(x), a.args)))


        if inp:
            rounds.append ((inp, resp, actions))

        self.ts.append((test_name, self.lang, prep, rounds, clause.location.fn, clause.location.line, clause.location.col))

    def extract_ner_training (self, clause):

        if len(clause.head.args) != 4:
            self.report_error ('train_ner: 4 arguments (+Lang, +Class, -Entity, -Label) expected')

        arg_Lang   = clause.head.args[0].name
        arg_Cls    = clause.head.args[1].name
        arg_Entity = clause.head.args[2].name
        arg_Label  = clause.head.args[3].name

        logging.info ('computing NER training data for %s [%s] ...' % (arg_Cls, arg_Lang))

        # cProfile.run('self.rt.search(clause)', 'mestats')
        # self.rt.set_trace(True)
        solutions = self.rt.search(clause)

        if not arg_Lang in self.ner:
            self.ner[arg_Lang] = {}

        if not arg_Cls in self.ner[arg_Lang]:
            self.ner[arg_Lang][arg_Cls] = {}
            
        ner = self.ner[arg_Lang][arg_Cls]

        cnt = 0
        for s in solutions:
            entity = s[arg_Entity].name
            label  = s[arg_Label].s

            ner[entity] = label
            cnt += 1

        logging.info ('computing NER training data for %s [%s] ... done. %d entries processed.' % (arg_Cls, arg_Lang, cnt))

