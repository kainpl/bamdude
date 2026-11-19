/**
 * The boxes the timelapse viewer and editor show their video in.
 *
 * A video given only `w-full` takes its height from its own shape: a portrait
 * recording in a wide window is taller than the screen and pushes the controls
 * below the fold. These boxes take the width they are given and are capped in
 * height; `object-contain` keeps the whole picture inside the box, letterboxed.
 *
 * Kept apart from the components so a geometry test can lay the same classes
 * out in a real browser.
 */

/**
 * The viewer. Its window is at most 90vh tall. On a narrow window the header and
 * the controls wrap onto second lines and need 16rem; from `sm` up they sit on
 * one line each and need 12rem (measured: 256px and 179px) — the difference is
 * most of the video on a phone held sideways.
 */
export const TIMELAPSE_VIEWER_VIDEO_CLASS =
  'block w-full max-h-[calc(90vh-16rem)] sm:max-h-[calc(90vh-12rem)] min-h-24 rounded-lg bg-black object-contain';

/** The editor: at most 45 % of the window's height, so the timeline under it stays on screen. */
export const TIMELAPSE_EDITOR_VIDEO_CLASS = 'block w-full max-h-[45vh] min-h-24 rounded-lg bg-black object-contain';
