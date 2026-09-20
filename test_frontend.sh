#!/bin/sh

cd frontend
npm run typecheck   # tsc -b --noEmit; a bare `npx tsc` enters no file here and exits 0
npm run lint
npm run i18n:check
npm run test:run
cd ..
