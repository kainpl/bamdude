import { useState, useRef, useEffect, useId, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Save,
  CopyPlus,
  Film,
  Play,
  Pause,
  Scissors,
  Gauge,
  Music,
  Upload,
  Trash2,
  Volume2,
  VolumeX,
  Loader2,
  AlertTriangle,
} from 'lucide-react';
import { Button } from './Button';
import { Modal } from './Modal';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { formatMediaTime } from '../utils/date';
import { useTimelapseEditJob } from './timelapse/useTimelapseEditJob';
import { TIMELAPSE_EDITOR_VIDEO_CLASS } from './timelapse/videoBox';

interface TimelapseEditorModalProps {
  archiveId: number;
  timelapseSrc: string;
  onClose: () => void;
  onSave?: () => void;
}

const SPEED_OPTIONS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];
/** The server's cap on the music file; checked here only to refuse early, in words. */
const MUSIC_LIMIT_BYTES = 100 * 1024 * 1024;
const KNOWN_ERRORS = [
  'source_changed',
  'target_gone',
  'authority_lost',
  'interrupted',
  'encode_failed',
  'encode_timeout',
  'encode_stalled',
  'output_invalid',
  'process_unreaped',
  'publish_failed',
  'cleanup_failed',
];

/**
 * The timelapse editor: trim, speed, music.
 *
 * Saving starts a background edit on the server and this dialog follows it —
 * it can be closed meanwhile and opened again. *Save* replaces the video
 * attached to the print; *Save as new* keeps it and stores the result beside
 * it. Progress reaches 100 % only when the server has published the result.
 */
export function TimelapseEditorModal({
  archiveId,
  timelapseSrc,
  onClose,
  onSave,
}: TimelapseEditorModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const headingId = useId();
  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  // Video state
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  // Editor state
  const [trimStart, setTrimStart] = useState(0);
  const [trimEnd, setTrimEnd] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioVolume, setAudioVolume] = useState(0.8);
  const [audioMuted, setAudioMuted] = useState(false);

  const [starting, setStarting] = useState(false);
  const edit = useTimelapseEditJob(archiveId);
  const adoptEdit = edit.adopt;
  const job = edit.job;
  const phase = job?.phase ?? null;
  const busy = starting || edit.active;

  // Fetch video info
  const { data: videoInfo, isLoading: isLoadingInfo } = useQuery({
    queryKey: ['timelapse-info', archiveId],
    queryFn: () => api.getTimelapseInfo(archiveId),
  });

  // Fetch thumbnails
  const { data: thumbnailData } = useQuery({
    queryKey: ['timelapse-thumbnails', archiveId],
    queryFn: () => api.getTimelapseThumbnails(archiveId, 15),
  });

  const start = async (saveMode: 'replace' | 'new') => {
    if (busy) return;
    setStarting(true);
    try {
      adoptEdit(
        await api.startTimelapseEdit(
          archiveId,
          { trimStart, trimEnd, speed, saveMode, audioVolume, mute: audioMuted },
          audioFile || undefined,
        ),
      );
    } catch (error) {
      showToast(
        t('timelapseEditor.startFailed', { error: error instanceof Error ? error.message : String(error) }),
        'error',
      );
    } finally {
      setStarting(false);
    }
  };

  // A finished edit: said once, and a published result closes the editor — the
  // video it was showing is no longer the one the archive has.
  const finishedId = phase === 'done' || phase === 'cancelled' ? job?.job_id : undefined;
  useEffect(() => {
    if (!finishedId || !job) return;
    if (job.phase === 'done') {
      showToast(t(`timelapseEditor.done.${job.save_mode}`), 'success');
      onSave?.();
      onClose();
    } else {
      showToast(t('timelapseEditor.cancelled'), 'info');
      adoptEdit(null);
    }
    // Only the job's end triggers this; the callbacks are the caller's and may change every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishedId]);

  // Initialize trimEnd when duration is available
  useEffect(() => {
    if (videoInfo?.duration && trimEnd === 0) {
      setTrimEnd(videoInfo.duration);
    }
  }, [videoInfo?.duration, trimEnd]);

  // Video event handlers
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const handleTimeUpdate = () => {
      const time = video.currentTime;
      setCurrentTime(time);

      // Loop within trim region
      if (time >= trimEnd) {
        video.currentTime = trimStart;
      }
    };

    const handleDurationChange = () => {
      setDuration(video.duration);
      if (trimEnd === 0) {
        setTrimEnd(video.duration);
      }
    };

    const handlePlay = () => setIsPlaying(true);
    const handlePause = () => setIsPlaying(false);

    video.addEventListener('timeupdate', handleTimeUpdate);
    video.addEventListener('durationchange', handleDurationChange);
    video.addEventListener('play', handlePlay);
    video.addEventListener('pause', handlePause);

    return () => {
      video.removeEventListener('timeupdate', handleTimeUpdate);
      video.removeEventListener('durationchange', handleDurationChange);
      video.removeEventListener('play', handlePlay);
      video.removeEventListener('pause', handlePause);
    };
  }, [trimStart, trimEnd]);

  // Sync audio with video
  useEffect(() => {
    const audio = audioRef.current;
    const video = videoRef.current;
    if (!audio || !video || !audioUrl) return;

    audio.currentTime = video.currentTime;
    audio.playbackRate = video.playbackRate;

    if (isPlaying && !audioMuted) {
      audio.play().catch(() => {});
    } else {
      audio.pause();
    }
  }, [isPlaying, audioUrl, audioMuted]);

  // Update audio volume
  useEffect(() => {
    if (audioRef.current) {
      audioRef.current.volume = audioMuted ? 0 : audioVolume;
    }
  }, [audioVolume, audioMuted]);

  // Update playback rate
  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.playbackRate = speed;
    }
    if (audioRef.current) {
      audioRef.current.playbackRate = speed;
    }
  }, [speed]);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;

    if (isPlaying) {
      video.pause();
    } else {
      // Start from trim start if before it
      if (video.currentTime < trimStart) {
        video.currentTime = trimStart;
      }
      video.play();
    }
  }, [isPlaying, trimStart]);

  const handleSeek = (time: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(trimStart, Math.min(trimEnd, time));
  };

  const handleAudioUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > MUSIC_LIMIT_BYTES) {
      showToast(t('timelapseEditor.musicTooLarge'), 'error');
      e.target.value = '';
      return;
    }

    // Cleanup previous URL
    if (audioUrl) {
      URL.revokeObjectURL(audioUrl);
    }

    setAudioFile(file);
    setAudioUrl(URL.createObjectURL(file));
  };

  const removeAudio = () => {
    if (audioUrl) {
      URL.revokeObjectURL(audioUrl);
    }
    setAudioFile(null);
    setAudioUrl(null);
  };

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (audioUrl) {
        URL.revokeObjectURL(audioUrl);
      }
    };
  }, [audioUrl]);

  const trimmedDuration = trimEnd - trimStart;
  const outputDuration = trimmedDuration / speed;
  const percent = Math.round((job?.progress ?? 0) * 100);
  const failed = phase === 'failed' || edit.lost;
  const errorCode = job?.error ?? '';

  return (
    <Modal
      onClose={onClose}
      labelledBy={headingId}
      size="5xl"
      panelClassName="overflow-hidden"
      bodyClassName="flex flex-col"
      header={
        <div className="flex flex-1 flex-wrap items-center justify-between gap-2 min-w-0">
          <h3 id={headingId} className="text-lg font-semibold text-white flex items-center gap-2">
            <Film className="w-5 h-5 text-bambu-green" />
            {t('timelapseEditor.title')}
          </h3>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void start('new')}
              disabled={busy || isLoadingInfo}
              title={t('timelapseEditor.saveAsNewHint')}
            >
              <CopyPlus className="w-4 h-4" />
              {t('timelapseEditor.saveAsNew')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void start('replace')}
              disabled={busy || isLoadingInfo}
              title={t('timelapseEditor.saveHint')}
            >
              {starting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              {t('timelapseEditor.save')}
            </Button>
          </div>
        </div>
      }
    >
      {/* Content — one dialog for both states: the video info arrives into the
          same shell the editor sits in, rather than a second overlay of its
          own that would remount (and re-take focus) the moment it loaded. */}
      {isLoadingInfo ? (
        <div className="flex flex-1 items-center justify-center gap-3 text-white">
          <Loader2 className="w-6 h-6 animate-spin" />
          {t('timelapseEditor.loading')}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Video Preview */}
          <div className="relative">
            <video
              ref={videoRef}
              src={timelapseSrc}
              className={TIMELAPSE_EDITOR_VIDEO_CLASS}
              data-testid="timelapse-editor-video"
              onClick={togglePlay}
              muted={!!audioUrl || audioMuted}
            />

            {/* Play overlay */}
            {!isPlaying && (
              // The play control lies over the video preview inside this
              // dialog: it starts playback, it dismisses nothing.
              // not-a-modal: viewer
              <button
                onClick={togglePlay}
                aria-label={t('timelapseEditor.play')}
                className="absolute inset-0 flex items-center justify-center bg-black/30 hover:bg-black/40 transition-colors"
              >
                <div className="p-4 bg-bambu-green rounded-full">
                  <Play className="w-8 h-8 text-white" />
                </div>
              </button>
            )}

            {/* Hidden audio element for music overlay preview */}
            {audioUrl && (
              <audio ref={audioRef} src={audioUrl} loop />
            )}
          </div>

          {/* Timeline with Thumbnails */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm text-bambu-gray">
              <Scissors className="w-4 h-4" />
              <span>{t('timelapseEditor.trim')}</span>
              <span className="ml-auto">
                {formatMediaTime(trimStart)} - {formatMediaTime(trimEnd)} ({formatMediaTime(trimmedDuration)})
              </span>
            </div>

            {/* Thumbnail strip */}
            <div className="relative h-16 bg-bambu-dark rounded-lg overflow-hidden">
              {/* Thumbnails background */}
              <div className="absolute inset-0 flex">
                {thumbnailData?.thumbnails.map((thumb, i) => (
                  <div
                    key={i}
                    className="flex-1 bg-cover bg-center"
                    style={{
                      backgroundImage: `url(data:image/jpeg;base64,${thumb})`,
                    }}
                  />
                ))}
              </div>

              {/* Trim overlay - grayed out areas */}
              <div
                className="absolute inset-y-0 left-0 bg-black/60"
                style={{ width: `${(trimStart / duration) * 100}%` }}
              />
              <div
                className="absolute inset-y-0 right-0 bg-black/60"
                style={{ width: `${((duration - trimEnd) / duration) * 100}%` }}
              />

              {/* Selected region border */}
              <div
                className="absolute inset-y-0 border-2 border-bambu-green"
                style={{
                  left: `${(trimStart / duration) * 100}%`,
                  right: `${((duration - trimEnd) / duration) * 100}%`,
                }}
              />

              {/* Current time indicator */}
              <div
                className="absolute top-0 bottom-0 w-0.5 bg-white shadow-lg"
                style={{ left: `${(currentTime / duration) * 100}%` }}
              />

              {/* Trim handles */}
              <input
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={trimStart}
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  if (val < trimEnd - 1) {
                    setTrimStart(val);
                    if (videoRef.current && videoRef.current.currentTime < val) {
                      videoRef.current.currentTime = val;
                    }
                  }
                }}
                className="absolute inset-0 w-full opacity-0 cursor-ew-resize"
                style={{ clipPath: 'inset(0 50% 0 0)' }}
              />
              <input
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={trimEnd}
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  if (val > trimStart + 1) {
                    setTrimEnd(val);
                  }
                }}
                className="absolute inset-0 w-full opacity-0 cursor-ew-resize"
                style={{ clipPath: 'inset(0 0 0 50%)' }}
              />
            </div>

            {/* Playback scrubber */}
            <input
              type="range"
              min={0}
              max={duration}
              step={0.1}
              value={currentTime}
              onChange={(e) => handleSeek(parseFloat(e.target.value))}
              className="w-full h-1 bg-bambu-dark-tertiary rounded-lg appearance-none cursor-pointer
                [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3
                [&::-webkit-slider-thumb]:bg-bambu-green [&::-webkit-slider-thumb]:rounded-full
                [&::-webkit-slider-thumb]:cursor-pointer"
            />

            {/* Play controls */}
            <div className="flex items-center justify-center gap-2">
              <button
                onClick={togglePlay}
                aria-label={t(isPlaying ? 'timelapseEditor.pause' : 'timelapseEditor.play')}
                className="p-2 bg-bambu-green hover:bg-bambu-green-dark rounded-lg transition-colors"
              >
                {isPlaying ? (
                  <Pause className="w-5 h-5 text-white" />
                ) : (
                  <Play className="w-5 h-5 text-white" />
                )}
              </button>
            </div>
          </div>

          {/* Speed Control */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm text-bambu-gray">
              <Gauge className="w-4 h-4" />
              <span>{t('timelapseEditor.speed')}</span>
              <span className="ml-auto">
                {t('timelapseEditor.speedOutput', { speed, duration: formatMediaTime(outputDuration) })}
              </span>
            </div>
            <div className="flex flex-wrap gap-1">
              {SPEED_OPTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => setSpeed(s)}
                  className={`flex-1 min-w-12 px-2 py-2 text-sm rounded transition-colors ${
                    speed === s
                      ? 'bg-bambu-green text-white'
                      : 'bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary'
                  }`}
                >
                  {s}x
                </button>
              ))}
            </div>
          </div>

          {/* Audio Upload */}
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm text-bambu-gray">
              <Music className="w-4 h-4" />
              <span>{t('timelapseEditor.music')}</span>
              <button
                type="button"
                onClick={() => setAudioMuted(!audioMuted)}
                aria-pressed={audioMuted}
                title={t(audioMuted ? 'timelapseEditor.musicUnmute' : 'timelapseEditor.musicMute')}
                aria-label={t(audioMuted ? 'timelapseEditor.musicUnmute' : 'timelapseEditor.musicMute')}
                data-testid="timelapse-mute"
                className="ml-auto p-2 hover:bg-bambu-dark-tertiary rounded transition-colors"
              >
                {audioMuted ? (
                  <VolumeX className="w-4 h-4 text-bambu-gray" />
                ) : (
                  <Volume2 className="w-4 h-4 text-bambu-green" />
                )}
              </button>
            </div>

            {audioFile ? (
              <div className="flex items-center gap-3 p-3 bg-bambu-dark rounded-lg">
                <Music className="w-5 h-5 text-bambu-green" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-white truncate">{audioFile.name}</p>
                  <p className="text-xs text-bambu-gray">
                    {(audioFile.size / 1024 / 1024).toFixed(1)} MB
                  </p>
                </div>

                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.1}
                  value={audioVolume}
                  disabled={audioMuted}
                  aria-label={t('timelapseEditor.musicVolume')}
                  data-testid="timelapse-volume"
                  onChange={(e) => setAudioVolume(parseFloat(e.target.value))}
                  className="w-20 h-1 bg-bambu-dark-tertiary rounded-lg appearance-none cursor-pointer disabled:opacity-40
                    [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3
                    [&::-webkit-slider-thumb]:bg-bambu-green [&::-webkit-slider-thumb]:rounded-full"
                />

                <button
                  onClick={removeAudio}
                  aria-label={t('timelapseEditor.musicRemove')}
                  title={t('timelapseEditor.musicRemove')}
                  className="p-2 hover:bg-red-100 dark:hover:bg-red-500/20 rounded transition-colors"
                >
                  <Trash2 className="w-4 h-4 text-red-600 dark:text-red-400" />
                </button>
              </div>
            ) : (
              <label className="flex flex-col items-center justify-center gap-2 p-4 border-2 border-dashed border-bambu-dark-tertiary rounded-lg cursor-pointer hover:border-bambu-green/50 transition-colors">
                <Upload className="w-8 h-8 text-bambu-gray" />
                <span className="text-sm text-bambu-gray">
                  {t('timelapseEditor.musicDrop')}
                </span>
                <span className="text-xs text-bambu-gray/60">
                  {t('timelapseEditor.musicFormats')}
                </span>
                <input
                  type="file"
                  accept=".mp3,.wav,.m4a,.aac,.ogg,audio/*"
                  onChange={handleAudioUpload}
                  className="hidden"
                  data-testid="timelapse-music-input"
                />
              </label>
            )}
            {audioMuted && <p className="text-xs text-bambu-gray">{t('timelapseEditor.mutedNote')}</p>}
          </div>

          {/* Summary */}
          <div className="p-3 bg-bambu-dark rounded-lg text-sm space-y-1">
            <p className="text-bambu-gray">
              <span className="text-white">{t('timelapseEditor.summaryOriginal')}</span> {formatMediaTime(duration)} @ {videoInfo?.width}x{videoInfo?.height}
            </p>
            <p className="text-bambu-gray">
              <span className="text-white">{t('timelapseEditor.summaryOutput')}</span>{' '}
              {t('timelapseEditor.summaryOutputValue', { duration: formatMediaTime(outputDuration), speed })}
              {audioMuted ? t('timelapseEditor.summaryMuted') : audioFile ? t('timelapseEditor.summaryMusic') : ''}
            </p>
          </div>
        </div>
      )}

      {/* The edit in progress, or how it ended. Lies over the editor inside this dialog and dismisses
          nothing by a click outside it. */}
      {(edit.active || failed) && (
        // not-a-modal: loading
        <div
          className="absolute inset-0 bg-black/85 flex flex-col items-center justify-center gap-4 p-6 text-center"
          data-testid="timelapse-edit-status"
        >
          {failed ? (
            <>
              <AlertTriangle className="w-10 h-10 text-amber-400" />
              <p className="text-white text-lg">{t('timelapseEditor.failedTitle')}</p>
              <p className="text-bambu-gray text-sm max-w-md" data-testid="timelapse-edit-error">
                {edit.lost
                  ? t('timelapseEditor.lost')
                  : t(KNOWN_ERRORS.includes(errorCode) ? `timelapseEditor.error.${errorCode}` : 'timelapseEditor.error.other')}
              </p>
              <Button variant="secondary" size="sm" onClick={() => adoptEdit(null)}>
                {t('timelapseEditor.dismiss')}
              </Button>
            </>
          ) : (
            <>
              <Loader2 className="w-10 h-10 text-bambu-green animate-spin" />
              <p className="text-white text-lg">
                {t(`timelapseEditor.phase.${phase === 'encoding' || phase === 'publishing' || phase === 'cancelling' ? phase : 'queued'}`)}
              </p>
              <div
                className="w-full max-w-md h-2 rounded bg-bambu-dark-tertiary overflow-hidden"
                role="progressbar"
                aria-label={t('timelapseEditor.progressLabel')}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={percent}
              >
                <div className="h-full bg-bambu-green transition-all" style={{ width: `${percent}%` }} />
              </div>
              <p className="text-bambu-gray text-sm" data-testid="timelapse-edit-percent">{percent}%</p>
              <p className="text-bambu-gray text-xs max-w-md">{t('timelapseEditor.backgroundHint')}</p>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void edit.cancel()}
                disabled={phase === 'cancelling' || edit.cancelTooLate}
              >
                {t('timelapseEditor.cancel')}
              </Button>
              {edit.cancelTooLate && (
                <p className="text-amber-400 text-xs max-w-md" data-testid="timelapse-cancel-too-late">
                  {t('timelapseEditor.cancelTooLate')}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
