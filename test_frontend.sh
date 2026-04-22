#!/bin/sh

cd frontend
npx tsc
npm run lint
npm run i18n:check
npm run test:run
cd ..
