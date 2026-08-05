/**
 * The one surface where system tags stay hidden permanently.
 *
 * m128 made system tags rows in the same catalog as user tags, so
 * ``GET /library/tags`` returns both kinds. The filter row and the management
 * dialog show both DELIBERATELY — that is the merge. Assignment is different:
 * a system tag is derived from the file, and the backend drops system ids from
 * add/remove **silently**. Offering one as a checkbox would report success and
 * change nothing, which is worse than refusing.
 *
 * The filter row's own behaviour lives in FileManagerTagFilter.test.tsx.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { BulkTagsPickerModal } from '../../components/BulkTagsPickerModal';

const stamps = { created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' };

const TAGS = [
  { id: 1, name: '3MF', file_count: 12, is_system: true, code: '3mf', ...stamps },
  { id: 2, name: 'kid-safe', file_count: 4, is_system: false, code: null, ...stamps },
];

describe('system tags are never offered for assignment', () => {
  beforeEach(() => {
    server.use(http.get('/api/v1/library/tags', () => HttpResponse.json(TAGS)));
  });

  it('the bulk tag picker does not offer a system tag', async () => {
    render(<BulkTagsPickerModal open fileIds={[1]} onClose={() => {}} />);

    expect(await screen.findByText('kid-safe')).toBeInTheDocument();
    expect(screen.queryByText('3MF')).not.toBeInTheDocument();
  });
});
