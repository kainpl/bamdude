/**
 * Whether a stored address may become an `href`.
 *
 * The server stores only http(s) links, but an `href` is where a `javascript:`
 * address would RUN, so the place that renders one asks again: a value that
 * got into the database some other way stays text.
 */
export function isHttpLink(value: string | null | undefined): value is string {
  return typeof value === 'string' && /^https?:\/\/\S+$/i.test(value);
}
