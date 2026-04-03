import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Trash2, Edit2, Send, Loader2, CheckCircle, XCircle } from 'lucide-react';
import { api } from '../api/client';
import type { TelegramChat, TelegramChatUpdate } from '../api/client';
import { Card, CardContent } from './Card';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { Toggle } from './Toggle';

interface TelegramChatCardProps {
  chat: TelegramChat;
  onEdit: (chat: TelegramChat) => void;
}

export function TelegramChatCard({ chat, onEdit }: TelegramChatCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  const updateMutation = useMutation({
    mutationFn: (data: TelegramChatUpdate) => api.updateTelegramChat(chat.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['telegram-chats'] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteTelegramChat(chat.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['telegram-chats'] });
    },
  });

  const testMutation = useMutation({
    mutationFn: () => api.testTelegramChat(chat.id),
    onSuccess: () => {
      setTestResult({ success: true, message: t('telegram.testSuccess') });
      setTimeout(() => setTestResult(null), 3000);
    },
    onError: (err: Error) => {
      setTestResult({ success: false, message: err.message });
      setTimeout(() => setTestResult(null), 5000);
    },
  });

  const isPending = chat.group_id === null;
  const eventCount = chat.notify_events?.length ?? 7; // 7 = default count
  const totalEvents = 24;

  return (
    <>
      <Card>
        <CardContent className="py-3">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <div className={`w-2 h-2 rounded-full ${chat.is_active ? 'bg-green-500' : isPending ? 'bg-yellow-500' : 'bg-red-500'}`} />
              <span className="text-white font-medium text-sm">
                {chat.label || `Chat ${chat.chat_id}`}
              </span>
              {isPending && (
                <span className="text-xs text-yellow-400 bg-yellow-400/10 px-1.5 py-0.5 rounded">
                  {t('telegram.pending')}
                </span>
              )}
            </div>
            <div className="flex items-center gap-1">
              <Toggle
                checked={chat.is_active}
                onChange={(checked) => updateMutation.mutate({ is_active: checked })}
              />
              <button
                onClick={() => onEdit(chat)}
                className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded transition-colors"
                title={t('telegram.edit')}
              >
                <Edit2 className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={() => setShowDeleteConfirm(true)}
                className="p-1.5 text-bambu-gray hover:text-red-400 hover:bg-bambu-dark-tertiary rounded transition-colors"
                title={t('telegram.delete')}
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          <div className="space-y-1 text-xs text-bambu-gray">
            <div>Chat ID: <span className="text-white font-mono">{chat.chat_id}</span></div>
            <div>
              {t('telegram.user')}: <span className="text-white">{chat.username || '\u2014'}</span>
            </div>
            <div>
              {t('telegram.role')}: <span className="text-white">{chat.group_name || t('telegram.notAssigned')}</span>
            </div>
            <div>
              {t('telegram.events')}: <span className="text-white">
                {chat.notify_events === null ? t('telegram.defaults') : `${eventCount}/${totalEvents}`}
              </span>
              {chat.daily_digest && <span className="ml-1.5 text-emerald-400">+ {t('telegram.dailyDigest')}</span>}
              {chat.quiet_hours_enabled && (
                <span className="ml-1.5 text-indigo-400">
                  {t('notifications.quiet')} {chat.quiet_hours_start}–{chat.quiet_hours_end}
                </span>
              )}
            </div>
          </div>

          {/* Test button */}
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => testMutation.mutate()}
              disabled={testMutation.isPending || !chat.is_active}
            >
              {testMutation.isPending ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <Send className="w-3 h-3" />
              )}
              {t('telegram.test')}
            </Button>
            {testResult && (
              <span className={`text-xs flex items-center gap-1 ${testResult.success ? 'text-green-400' : 'text-red-400'}`}>
                {testResult.success ? <CheckCircle className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                {testResult.message}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {showDeleteConfirm && (
        <ConfirmModal
          title={t('telegram.deleteTitle')}
          message={t('telegram.deleteMessage', { label: chat.label || chat.chat_id })}
          confirmText={t('telegram.delete')}
          onConfirm={() => {
            deleteMutation.mutate();
            setShowDeleteConfirm(false);
          }}
          onCancel={() => setShowDeleteConfirm(false)}
          variant="danger"
        />
      )}
    </>
  );
}
