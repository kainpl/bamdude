import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { ThemeProvider, useTheme } from '../../contexts/ThemeContext';

describe('G01 A safe preference projection', () => {
  it('loads the saved theme without reading full settings when settings:read is denied', async () => {
    let privateReads = 0;
    server.use(
      http.get('/api/v1/settings/', () => { privateReads++; return HttpResponse.json({ detail: 'Forbidden' }, { status: 403 }); }),
      http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ dark_style: 'glow', dark_background: 'forest', dark_accent: 'blue' })),
    );
    const { result } = renderHook(() => useTheme(), { wrapper: ThemeProvider });
    await waitFor(() => expect(result.current.darkStyle).toBe('glow'));
    expect(result.current.darkBackground).toBe('forest');
    expect(privateReads).toBe(0);
  });
});
