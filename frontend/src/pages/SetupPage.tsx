import { useState } from 'react';
import { useNavigate } from 'react-router';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, setAuthToken } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import { Info } from 'lucide-react';
import { PasswordField } from '../components/PasswordField';
import { checkPasswordComplexity } from '../utils/password';

export function SetupPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { mode } = useTheme();
  const { refreshAuth } = useAuth();
  const [adminUsername, setAdminUsername] = useState('');
  const [adminEmail, setAdminEmail] = useState('');
  const [adminPassword, setAdminPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  const setupMutation = useMutation({
    mutationFn: () =>
      api.setupAuth({
        admin_username: adminUsername.trim(),
        admin_password: adminPassword,
        admin_email: adminEmail.trim() || undefined,
      }),
    onSuccess: async (data) => {
      // Backend creates the admin and returns a JWT - store it and land the
      // user straight on the home page without a detour through /login.
      setAuthToken(data.access_token);
      await refreshAuth();
      showToast(t('setup.toast.authEnabledAdminCreated'));
      navigate('/');
    },
    onError: (error: Error) => {
      showToast(error.message, 'error');
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    if (!adminUsername.trim() || !adminPassword) {
      showToast(t('setup.toast.enterBothCredentials'), 'error');
      return;
    }
    if (adminPassword !== confirmPassword) {
      showToast(t('setup.toast.passwordsDoNotMatch'), 'error');
      return;
    }
    const ruleKey = checkPasswordComplexity(adminPassword);
    if (ruleKey) {
      showToast(t(ruleKey), 'error');
      return;
    }

    setupMutation.mutate();
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-bambu-dark p-4">
      <div className="max-w-md w-full space-y-8 p-8 bg-gradient-to-br from-bambu-card to-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary shadow-lg">
        <div className="text-center">
          <div className="flex items-center justify-center mb-4">
            <img
              src={mode === 'dark' ? '/img/brand/lockup-compact-on-dark.svg' : '/img/brand/lockup-compact-on-light.svg'}
              alt="BamDude"
              className="h-12 w-auto"
            />
          </div>
          <h2 className="text-3xl font-bold text-white">
            {t('setup.title')}
          </h2>
          <p className="mt-2 text-sm text-bambu-gray">
            {t('setup.subtitle')}
          </p>
        </div>

        <form className="mt-4 space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div className="p-3 bg-bambu-dark-secondary/50 border border-bambu-dark-tertiary rounded-lg">
              <div className="flex items-start gap-2">
                <Info className="w-4 h-4 text-bambu-green mt-0.5 flex-shrink-0" />
                <div className="text-sm text-bambu-gray">
                  <p className="text-white font-medium mb-1">{t('setup.adminAccount')}</p>
                  <p>
                    {t('setup.adminAccountDesc')}
                  </p>
                </div>
              </div>
            </div>

            <div>
              <label htmlFor="admin-username" className="block text-sm font-medium text-white mb-2">
                {t('setup.adminUsername')}
              </label>
              <input
                id="admin-username"
                type="text"
                value={adminUsername}
                onChange={(e) => setAdminUsername(e.target.value)}
                className="block w-full px-4 py-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors"
                placeholder={t('setup.adminUsernamePlaceholder')}
                autoComplete="username"
                required
              />
            </div>

            <div>
              <label htmlFor="admin-email" className="block text-sm font-medium text-white mb-2">
                {t('setup.adminEmail')} <span className="text-bambu-gray text-xs">{t('setup.optional')}</span>
              </label>
              <input
                id="admin-email"
                type="email"
                value={adminEmail}
                onChange={(e) => setAdminEmail(e.target.value)}
                className="block w-full px-4 py-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors"
                placeholder={t('setup.adminEmailPlaceholder')}
                autoComplete="email"
              />
            </div>

            <PasswordField
              id="admin-password"
              label={t('setup.adminPassword')}
              value={adminPassword}
              onChange={setAdminPassword}
              placeholder={t('setup.adminPasswordPlaceholder')}
              required
              showRules
            />

            <PasswordField
              id="confirm-password"
              label={t('setup.confirmPassword')}
              value={confirmPassword}
              onChange={setConfirmPassword}
              placeholder={t('setup.confirmPasswordPlaceholder')}
              required
              mustMatch={adminPassword}
            />
          </div>

          <div>
            <button
              type="submit"
              disabled={setupMutation.isPending}
              className="w-full flex justify-center py-3 px-4 bg-bambu-green hover:bg-bambu-green-light text-white font-medium rounded-lg shadow-lg shadow-bambu-green/20 hover:shadow-bambu-green/30 focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:ring-offset-2 focus:ring-offset-bambu-dark-secondary transition-all disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-bambu-green"
            >
              {setupMutation.isPending ? t('setup.settingUp') : t('setup.completeSetup')}
            </button>
            <p className="text-xs text-bambu-gray text-center">
              {t('setup.telemetryNotice')}
            </p>
          </div>
        </form>
      </div>
    </div>
  );
}
