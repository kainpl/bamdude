/**
 * What opens a library file, and which files get a generated thumbnail — both
 * decided by type, in one place each.
 */
import { describe, it, expect } from 'vitest';
import { previewKindOf } from '../../utils/filePreview';
import {
  isCadThumbnailType,
  isLegacyThumbnailType,
  isPdfThumbnailType,
  isThumbnailUploadName,
  offersThumbnail,
} from '../../utils/meshPreviewTypes';

describe('previewKindOf', () => {
  it('opens a PDF in the PDF viewer', () => {
    expect(previewKindOf({ filename: 'manual.pdf', file_type: 'pdf' })).toBe('pdf');
    expect(previewKindOf({ filename: 'MANUAL.PDF', file_type: 'pdf' })).toBe('pdf');
  });

  it('keeps the other answers', () => {
    expect(previewKindOf({ filename: 'a.png', file_type: 'png' })).toBe('image');
    expect(previewKindOf({ filename: 'a.3mf', file_type: '3mf' })).toBe('model');
    expect(previewKindOf({ filename: 'a.txt', file_type: 'txt' })).toBeNull();
  });
});

describe('thumbnail types', () => {
  it('counts a PDF among the files a thumbnail is generated for', () => {
    expect(isPdfThumbnailType('pdf')).toBe(true);
    expect(isPdfThumbnailType('PDF')).toBe(true);
    expect(isPdfThumbnailType('stl')).toBe(false);
    expect(offersThumbnail('pdf')).toBe(true);
    expect(offersThumbnail('stl')).toBe(true);
    expect(offersThumbnail('step')).toBe(true);
    expect(offersThumbnail('3mf')).toBe(false);
    expect(offersThumbnail(null)).toBe(false);
  });

  it('leaves a PDF out of the mesh and CAD groups — its picture is not theirs', () => {
    expect(isLegacyThumbnailType('pdf')).toBe(false);
    expect(isCadThumbnailType('pdf')).toBe(false);
  });

  it('offers the upload checkbox for a PDF by its name', () => {
    expect(isThumbnailUploadName('Manual.PDF')).toBe(true);
    expect(isThumbnailUploadName('part.stl')).toBe(true);
    expect(isThumbnailUploadName('part.step')).toBe(true);
    expect(isThumbnailUploadName('part.3mf')).toBe(false);
  });
});
