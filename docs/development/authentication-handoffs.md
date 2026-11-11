# Authentication handoffs

G01 F is a review candidate on `audit/g01-f`, pending acceptance and merge.

`GET /api/v1/auth/oidc/authorize/{provider_id}` accepts optional boolean `remember_me`, default false. Manual sign-in sends the selected checkbox value; automatic SSO redirects always send false. The server binds this choice to its one-time state, then the callback exchange and any required second factor. Callback query parameters and a 2FA request body cannot override an OIDC attempt's choice. Existing safe post-login redirects remain supported.

TOTP, email OTP (including resend) and backup codes all issue refresh cookies after successful verification. An unchecked cookie lasts for the browser session; a checked cookie has a persistent Max-Age. Both refresh expiry and persistent-cookie lifetime respect the current administrator session ceiling on login and rotation. Access-token lifetime is unchanged.

The additive `auth_remember_me` migration adds a nullable boolean without backfill or default. Existing OIDC state/exchange rows use false; existing local pre-auth flows retain their client preference contract. New refresh rows carry an explicit boolean, so a short ceiling cannot erase a checked choice. Legacy refresh rows without a flag retain their previous lifespan inference until rotation. No credential retirement or new account/session identity is introduced by this unit.

LDAP service and user binds use the configured transport without retrying requested TLS as plaintext. Schema information is read after authenticated service bind. A configured CA file remains authoritative for both Test and login; explicitly configured legacy plaintext remains supported. LDAP Test failures include an optional safe `error` category (StartTLS refusal, TLS failure, bind, connection, search, groups or configuration), with localized UI text and no driver exception or credential echo.

Group lookup uses supported memberOf, member, uniqueMember and POSIX membership methods in the directory naming context. A complete empty result retains the configured default-group policy. A partial, refused, time/size-limited or referred membership result refuses the LDAP attempt before account/group changes. Existing manual assignments outside the LDAP-managed set remain untouched; a newly provisioned user does not receive a duplicate sync.

Validation uses signed mocked IdP callbacks, offline LDAP schema/directory fixtures and disposable SQLite/PostgreSQL databases. It does not claim compatibility acceptance against a live operator directory or identity provider. G01 C/D identity and continuous revocation remain separate units.
