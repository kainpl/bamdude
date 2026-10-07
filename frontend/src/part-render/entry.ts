/** Node entry of the part-render bundle (spec §5.5-5.6): the bundle is the entry, so it always runs. */
import { main } from './io';

main(process.stdin, process.stdout).then((code) => {
  process.exitCode = code;
});
