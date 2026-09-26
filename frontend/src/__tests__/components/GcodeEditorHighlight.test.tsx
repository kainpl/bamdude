/**
 * The G-code highlighter's output, pinned before its number rule lost its
 * lookbehind (audit D12 — Safari before 16.4 cannot parse one, and the whole
 * app would not load). The rewrite must render every line exactly as before.
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { GcodeEditor } from '../../components/GcodeEditor';

const LINES = [
  'G1 X10.5 Y-2 F3000 ; move to start',
  'M104 S200',
  'G28',
  'T1',
  'X1.5Y2',
  'text 12 34 5.5',
  'M117 Layer 3 of 120',
  '=5 "7" >8',
  '1,2;3',
  '; comment only 42',
  'G2 I1.25 J-0.5 R3',
  '=1.5 a=12.5 b >7.25 "3.5"',
  'S1.5 x 2.',
];

function highlight(code: string): string {
  const { container } = render(<GcodeEditor value={code} onChange={() => {}} />);
  return container.querySelector('pre')!.innerHTML;
}

describe('G-code highlighting', () => {
  it('renders every line exactly as before the number rule was rewritten', () => {
    expect(highlight(LINES.join('\n'))).toMatchInlineSnapshot(`
      "<span class="gc-g">G1</span> <span class="gc-param">X</span><span class="gc-num">10.5</span> <span class="gc-param">Y</span><span class="gc-num">-2</span> <span class="gc-param">F</span><span class="gc-num">3000</span> <span class="gc-comment">; move to start</span>
      <span class="gc-m">M104</span> <span class="gc-param">S</span><span class="gc-num">200</span>
      <span class="gc-g">G28</span>
      <span class="gc-param">T</span><span class="gc-num">1</span>
      <span class="gc-param">X</span><span class="gc-num">1.</span>5Y2
      text <span class="gc-num">12</span> <span class="gc-num">34</span> <span class="gc-num">5.5</span>
      <span class="gc-m">M117</span> Layer <span class="gc-num">3</span> of <span class="gc-num">120</span>
      =5 "7" &gt;<span class="gc-num">8</span>
      <span class="gc-num">1</span>,<span class="gc-num">2</span><span class="gc-comment">;3</span>
      <span class="gc-comment">; comment only 42</span>
      <span class="gc-g">G2</span> <span class="gc-param">I</span><span class="gc-num">1.25</span> <span class="gc-param">J</span><span class="gc-num">-<span class="gc-num">0.</span>5</span> <span class="gc-param">R</span><span class="gc-num">3</span>
      =1.<span class="gc-num">5</span> a=12.<span class="gc-num">5</span> b &gt;<span class="gc-num">7.25</span> "3.<span class="gc-num">5</span>"
      <span class="gc-param">S</span><span class="gc-num">1.5</span> x <span class="gc-num">2</span>.
      "
    `);
  });
});
