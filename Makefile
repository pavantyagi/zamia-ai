SHELL := /bin/bash

all:	prolog nlp_train

prolog:
	./gen_weather_data.py
	./prolog_compile.py data/src/common-sense.pl
	./prolog_compile.py data/dst/weather-dynamic.pl
	./prolog_compile.py -t data/dst/weather.ts -s data/dst/weather.sem data/src/weather.pl 
	./prolog_compile.py -t data/dst/greetings.ts -s data/dst/greetings.sem data/src/greetings.pl 
	./prolog_compile.py -t data/dst/radio.ts -s data/dst/radio.sem data/src/radio.pl 

nlp_train:
	./nlp_train_keras.py

kaldi:
	rm -rf data/dst/speech/de/kaldi
	./speech_kaldi_export.py
	# pushd data/dst//speech/kaldi
	# ./run.sh
	# popd

sphinx:
	rm -rf data/dst/speech/de/cmusphinx
	./speech_sphinx_export.py 
	pushd data/dst/speech/de/cmusphinx && ./sphinx-run.sh && popd
	
sequitur:
	rm -rf data/dst/speech/de/sequitur/
	./speech_sequitur_export.py
	./speech_sequitur_train.sh

stats:
	./speech_stats.py

clean:
	./clean_clauses.py
	rm -rf data/dst/*