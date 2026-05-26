#!/bin/bash

cd ../bamdude.top
git add .
git commit -m "Updated website"
git push

cd ../docs.bamdude.top
git add .
git commit -m "Updated Wiki"
git push

cd ../bamdude-telemetry/
git add .
git commit -m "Updated Stats"
git push
