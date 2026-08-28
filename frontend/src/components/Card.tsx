import type { ReactNode, MouseEvent, HTMLAttributes } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
  className?: string;
  onClick?: (e: MouseEvent) => void;
  onContextMenu?: (e: MouseEvent) => void;
  /** Opt out of the automatic pointer cursor. For a handler that only acts
   *  on MODIFIER clicks (Ctrl/Shift selection): a plain click does nothing
   *  there, and a whole card advertising itself as a link is a lie. */
  pointer?: boolean;
}

export function Card({ children, className = '', onClick, onContextMenu, pointer = true, ...rest }: CardProps) {
  return (
    <div
      // ⚠️ Conditional on an onClick actually being passed. A card that does
      // nothing must not advertise itself as clickable, and most of them do
      // nothing.
      className={`bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary card-shadow ${onClick && pointer ? 'cursor-pointer' : ''} ${className}`}
      onClick={onClick}
      onContextMenu={onContextMenu}
      {...rest}
    >
      {children}
    </div>
  );
}

export function CardHeader({ children, className = '' }: CardProps) {
  return (
    <div className={`px-6 py-4 border-b border-bambu-dark-tertiary ${className}`}>
      {children}
    </div>
  );
}

export function CardContent({ children, className = '' }: CardProps) {
  return <div className={`p-6 ${className}`}>{children}</div>;
}
