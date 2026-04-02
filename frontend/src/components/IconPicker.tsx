import { useState } from 'react';
import {
  Globe,
  Link,
  ExternalLink,
  Book,
  FileText,
  Home,
  Star,
  Heart,
  Bookmark,
  ShoppingCart,
  Music,
  Video,
  Image,
  Camera,
  Map,
  Compass,
  Coffee,
  Gift,
  Wrench,
  Zap,
  Cloud,
  Database,
  Folder,
  Mail,
  Phone,
  User,
  Users,
  Server,
  Terminal,
  Code,
  type LucideIcon,
} from 'lucide-react';

// Available icons for external links
export const AVAILABLE_ICONS: { name: string; icon: LucideIcon }[] = [
  { name: 'globe', icon: Globe },
  { name: 'link', icon: Link },
  { name: 'external-link', icon: ExternalLink },
  { name: 'book', icon: Book },
  { name: 'file-text', icon: FileText },
  { name: 'home', icon: Home },
  { name: 'star', icon: Star },
  { name: 'heart', icon: Heart },
  { name: 'bookmark', icon: Bookmark },
  { name: 'shopping-cart', icon: ShoppingCart },
  { name: 'music', icon: Music },
  { name: 'video', icon: Video },
  { name: 'image', icon: Image },
  { name: 'camera', icon: Camera },
  { name: 'map', icon: Map },
  { name: 'compass', icon: Compass },
  { name: 'coffee', icon: Coffee },
  { name: 'gift', icon: Gift },
  { name: 'wrench', icon: Wrench },
  { name: 'zap', icon: Zap },
  { name: 'cloud', icon: Cloud },
  { name: 'database', icon: Database },
  { name: 'folder', icon: Folder },
  { name: 'mail', icon: Mail },
  { name: 'phone', icon: Phone },
  { name: 'user', icon: User },
  { name: 'users', icon: Users },
  { name: 'server', icon: Server },
  { name: 'terminal', icon: Terminal },
  { name: 'code', icon: Code },
];

// Helper to get icon component by name
export function getIconByName(name: string): LucideIcon {
  const found = AVAILABLE_ICONS.find((i) => i.name === name);
  return found?.icon || Link;
}

interface IconPickerProps {
  value: string;
  onChange: (value: string) => void;
}

export function IconPicker({ value, onChange }: IconPickerProps) {
  const [isOpen, setIsOpen] = useState(false);

  const SelectedIcon = getIconByName(value);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white hover:border-bambu-gray focus:border-bambu-green focus:outline-none w-full"
      >
        <SelectedIcon className="w-5 h-5" />
        <span className="text-sm text-bambu-gray flex-1 text-left">{value}</span>
      </button>

      {isOpen && (
        <>
          {/* Backdrop */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => setIsOpen(false)}
          />

          {/* Dropdown */}
          <div className="absolute z-50 mt-1 w-full max-h-64 overflow-y-auto bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg">
            <div className="grid grid-cols-5 gap-1 p-2">
              {AVAILABLE_ICONS.map(({ name, icon: Icon }) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => {
                    onChange(name);
                    setIsOpen(false);
                  }}
                  className={`p-2 rounded-lg transition-colors flex items-center justify-center ${
                    value === name
                      ? 'bg-bambu-green text-white'
                      : 'hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
                  }`}
                  title={name}
                >
                  <Icon className="w-5 h-5" />
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
