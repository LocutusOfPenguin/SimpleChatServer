#!/usr/bin/env bash
virtualenv -p python3 chesspython
source chesspython/bin/activate
pip3 install --upgrade -r requirements.txt
exit
