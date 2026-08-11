/**
 * Which projects may be offered for binding.
 *
 * Archiving a project is the explicit "stop showing me this" action — the same
 * meaning it carries for printers, where an archived one leaves every place
 * that offers a choice instead of being labelled in small print.
 *
 * ⚠️ **The trap this is mostly here to pin:** a project already bound must stay
 * on the list even when archived. Hiding it renders the field as "nothing
 * chosen", and the next save writes that emptiness back — the filter would
 * quietly destroy the binding it was meant to tidy around.
 *
 * ⚠️ **`completed` is still offered.** Only `archived` means "put away"; a
 * finished project can still take a reprint.
 */

import { describe, it, expect } from 'vitest';
import { selectableProjects } from '../../utils/projects';

const ACTIVE = { id: 1, status: 'active' };
const COMPLETED = { id: 2, status: 'completed' };
const ARCHIVED = { id: 3, status: 'archived' };
const ALL = [ACTIVE, COMPLETED, ARCHIVED];

describe('selectableProjects', () => {
  it('drops archived projects', () => {
    expect(selectableProjects(ALL).map((p) => p.id)).toEqual([1, 2]);
  });

  it('keeps completed ones, which are finished rather than put away', () => {
    expect(selectableProjects(ALL).map((p) => p.id)).toContain(2);
  });

  it('keeps an archived project that is already bound', () => {
    expect(selectableProjects(ALL, [3]).map((p) => p.id)).toEqual([1, 2, 3]);
  });

  it('keeps it when the binding is a Set, as the multi-select dialogs hold it', () => {
    expect(selectableProjects(ALL, new Set([3])).map((p) => p.id)).toEqual([1, 2, 3]);
  });

  it('does not resurrect an archived project that is not bound', () => {
    expect(selectableProjects(ALL, [1]).map((p) => p.id)).toEqual([1, 2]);
  });

  it('answers an empty list for nothing at all', () => {
    expect(selectableProjects(undefined)).toEqual([]);
    expect(selectableProjects(null)).toEqual([]);
  });

  it('leaves a project with no status alone', () => {
    /* Only an explicit "archived" hides anything — an absent status is not a
       reason to drop a row from a picker. */
    expect(selectableProjects([{ id: 9 }]).map((p) => p.id)).toEqual([9]);
  });
});
