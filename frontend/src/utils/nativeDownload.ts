/**
 * Hand a same-origin URL to the browser's own download manager.
 *
 * The file never passes through JavaScript: no `fetch`, no Blob, no object
 * URL — a timelapse folder is gigabytes. The `download` attribute matters: with
 * it a same-origin link is always SAVED, so a refusal (the server answers a
 * small `download-failed.txt`) can never navigate the app away. The name comes
 * from the response's Content-Disposition.
 */
export function startNativeDownload(url: string): void {
  const link = document.createElement('a');
  link.href = url;
  link.download = '';
  link.rel = 'noopener';
  link.style.display = 'none';
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
  }
}
