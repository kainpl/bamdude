import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { NumberInput } from '../../components/NumberInput';

/** A parent that keeps the reported value, as every real caller does. */
function Held({
  initial,
  onReport,
  ...props
}: {
  initial: number;
  onReport?: (v: number) => void;
  min?: number;
  max?: number;
  integer?: boolean;
  fallback?: number;
  onCommit?: (v: number) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <NumberInput
      aria-label="field"
      value={value}
      onChange={(v) => {
        setValue(v);
        onReport?.(v);
      }}
      {...props}
    />
  );
}

const field = () => screen.getByRole('spinbutton', { name: 'field' }) as HTMLInputElement;
const type = (text: string) => fireEvent.change(field(), { target: { value: text } });

describe('NumberInput', () => {
  it('lets 60 be typed into a field with a minimum of 45 without snapping on the way', () => {
    const onReport = vi.fn();
    render(<Held initial={50} min={45} max={85} integer onReport={onReport} />);

    field().focus();
    type('');
    expect(field().value).toBe('');
    type('6');
    expect(field().value).toBe('6');
    expect(onReport).not.toHaveBeenCalled();
    type('60');
    expect(field().value).toBe('60');
    expect(onReport).toHaveBeenCalledTimes(1);
    expect(onReport).toHaveBeenCalledWith(60);
  });

  it('settles an emptied field on its fallback when it is left', () => {
    const onReport = vi.fn();
    render(<Held initial={50} min={45} max={85} fallback={45} onReport={onReport} />);

    type('');
    expect(onReport).not.toHaveBeenCalled();
    fireEvent.blur(field());
    expect(onReport).toHaveBeenCalledWith(45);
    expect(field().value).toBe('45');
  });

  it('goes back to the last value when emptied without a fallback', () => {
    const onReport = vi.fn();
    render(<Held initial={50} min={45} onReport={onReport} />);

    type('');
    fireEvent.blur(field());
    expect(onReport).not.toHaveBeenCalled();
    expect(field().value).toBe('50');
  });

  it('reports null for an emptied nullable field and keeps it empty', () => {
    const onChange = vi.fn();
    function Nullable() {
      const [value, setValue] = useState<number | null>(30);
      return (
        <NumberInput
          aria-label="field"
          allowEmpty
          min={1}
          value={value}
          onChange={(v) => {
            setValue(v);
            onChange(v);
          }}
        />
      );
    }
    render(<Nullable />);

    type('');
    expect(onChange).toHaveBeenCalledWith(null);
    fireEvent.blur(field());
    expect(field().value).toBe('');
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('clamps an out-of-range number when the field is left, not before', () => {
    const onReport = vi.fn();
    render(<Held initial={50} min={45} max={85} onReport={onReport} />);

    type('200');
    expect(field().value).toBe('200');
    expect(onReport).not.toHaveBeenCalled();
    fireEvent.blur(field());
    expect(onReport).toHaveBeenCalledWith(85);
    expect(field().value).toBe('85');

    type('3');
    fireEvent.keyDown(field(), { key: 'Enter' });
    expect(onReport).toHaveBeenLastCalledWith(45);
    expect(field().value).toBe('45');
  });

  it('accepts 0 where the minimum is 0, whatever the fallback', () => {
    const onReport = vi.fn();
    render(<Held initial={10} min={0} max={120} integer fallback={10} onReport={onReport} />);

    type('0');
    expect(onReport).toHaveBeenCalledWith(0);
    fireEvent.blur(field());
    expect(field().value).toBe('0');
    expect(onReport).toHaveBeenCalledTimes(1);
  });

  it('reads whole numbers only when asked to', () => {
    const onReport = vi.fn();
    render(<Held initial={1} integer onReport={onReport} />);
    type('2.7');
    expect(onReport).toHaveBeenCalledWith(2);
    fireEvent.blur(field());
    expect(field().value).toBe('2');
  });

  it('shows an outside change of the value while it is not being edited', () => {
    const { rerender } = render(<NumberInput aria-label="field" value={5} onChange={() => {}} />);
    expect(field().value).toBe('5');
    rerender(<NumberInput aria-label="field" value={9} onChange={() => {}} />);
    expect(field().value).toBe('9');
  });

  it('lets a value changed from outside win over what is being typed', () => {
    function WithReset() {
      const [value, setValue] = useState(10);
      return (
        <>
          <NumberInput aria-label="field" min={1} max={999} integer value={value} onChange={setValue} />
          <button onClick={() => setValue(6)}>reset</button>
        </>
      );
    }
    render(<WithReset />);

    type('25');
    expect(field().value).toBe('25');
    // No blur in between: a click that does not take focus still moves the value.
    fireEvent.click(screen.getByRole('button', { name: 'reset' }));
    expect(field().value).toBe('6');
  });

  it('shows the saved value again when the parent refuses what was reported', () => {
    render(<NumberInput aria-label="field" value={5} onChange={() => {}} />);
    type('7');
    expect(field().value).toBe('5');
  });

  it('commits once when the edit ends, never per key', () => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="field" value={5} min={1} max={60} integer fallback={5} onCommit={onCommit} />);

    type('');
    type('1');
    type('12');
    expect(onCommit).not.toHaveBeenCalled();
    fireEvent.blur(field());
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(12);
  });

  it('does not commit an edit that ended where it began', () => {
    const onCommit = vi.fn();
    render(<NumberInput aria-label="field" value={5} min={1} max={60} fallback={5} onCommit={onCommit} />);

    type('7');
    type('5');
    fireEvent.blur(field());
    type('');
    fireEvent.blur(field());
    expect(onCommit).not.toHaveBeenCalled();
  });

  it('passes native attributes through', () => {
    render(
      <NumberInput
        aria-label="field"
        id="the-field"
        data-testid="hook"
        className="w-20"
        placeholder="45"
        step={5}
        min={45}
        max={85}
        disabled
        value={null}
        onChange={() => {}}
      />,
    );
    const el = screen.getByTestId('hook') as HTMLInputElement;
    expect(el.id).toBe('the-field');
    expect(el.className).toBe('w-20');
    expect(el.placeholder).toBe('45');
    expect(el.type).toBe('number');
    expect(el.min).toBe('45');
    expect(el.max).toBe('85');
    expect(el.step).toBe('5');
    expect(el.disabled).toBe(true);
    expect(el.value).toBe('');
  });
});
