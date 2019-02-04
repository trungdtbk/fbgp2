#!/bin/bash

cd $TRAVIS_BUILD_DIR/faucet
python3 setup.py -q sdist
cd ..

