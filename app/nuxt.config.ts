export default defineNuxtConfig({
  devtools: { enabled: true },

  modules: [
    '@nuxt/ui',
    '@nuxt/eslint',
    'nuxt-auth-utils',
  ],

  css: ['~/assets/css/main.css'],

  colorMode: {
    preference: 'dark',
    fallback: 'dark',
  },

  runtimeConfig: {
    // nuxt-auth-utils session cookie defaults. `secure: false` by default
    // because the stand runs HTTP-only (Caddy on :80 without TLS). For prod
    // with HTTPS, set NUXT_SESSION_COOKIE_SECURE=true.
    session: {
      maxAge: 60 * 60 * 24,
      cookie: {
        sameSite: 'lax',
        secure: false,
      },
    },
    // --- OIDC / Authentik -----------------------------------------------------
    // Заполняется стадией 30-frontend из блока `sso:` в config.yaml:
    //   NUXT_OAUTH_OIDC_CLIENT_ID / _CLIENT_SECRET / _OPENID_CONFIG / _REDIRECT_URL
    // Обработчик — server/routes/auth/authentik.get.ts (generic OIDC из
    // nuxt-auth-utils: state + PKCE + nonce, в отличие от провайдера `authentik`
    // из той же библиотеки, где нет ни того, ни другого).
    oauth: {
      oidc: {
        // ⚠️ 'openid' здесь НЕТ намеренно. defineOAuthOidcEventHandler делает
        // defu(config, runtimeConfig, { scope: ['openid'] }), а defu массивы
        // СКЛЕИВАЕТ, а не перекрывает: напиши здесь 'openid' — уедет
        // "openid profile email openid". Базовый scope добавит сам обработчик.
        // Тот же defu склеивает scope и МЕЖДУ запросами, если обработчик живёт
        // дольше запроса — см. комментарий в server/routes/auth/authentik.get.ts.
        scope: ['profile', 'email'],
      },
    },
    // Настройки SSO, не относящиеся к самому протоколу. Секретов тут нет,
    // кроме allowedSubs (не секрет, но и светить в браузере незачем).
    sso: {
      // uuid'ы пользователей Authentik, которым разрешён вход, в виде
      // «<sub>» или «<sub>:<локальный логин>» через запятую. Пустая строка —
      // не пустит НИКОГО: новые учётки из IdP панель не заводит никогда.
      allowedSubs: '',
      // Парольный вход. По стандарту флота он не удаляется, а прячется с формы
      // (см. public.sso.autoRedirect) — false здесь рубит его наглухо.
      passwordLogin: true,
    },
    databaseUrl: 'file:./local.db',
    anysdaConfigPath: '/etc/anysda/config.yaml',
    routesFilePath: '/etc/anysda/manual-routes.json',
    vmUrl: 'http://127.0.0.1:8428',
    clashApiUrl: 'http://10.99.0.1:9090',
    clashSecret: '',
    aghUrl: 'http://127.0.0.1:3000',
    aghUser: '',
    aghPassword: '',
    exitTags: '',
    mgmtMeshIpPrefix: '10.99.0.',
    // Stable tag→mgmt_ip mapping, "tag:ip,tag:ip,...". Source of truth for
    // nodeInstances() — индексы не пересчитываются при exclusion экзитов
    // (см. vm-client.ts комментарий). Заполняется в 30-frontend.sh из
    // envs/all.env (MGMT_IP_*).
    mgmtIps: '',
    tgbotEventPort: 8877,
    tgbotSecret: '',
    wgEnabled: true,
    wgListenPort: 51820,
    wgServerIp: '10.66.66.1',
    wgSubnetPrefix: '10.66.66.',
    wgPublicHost: '',
    wgDns: '10.99.0.1',
    wgMtu: 1420,
    // Сплит-туннель: локальные сети клиента (RFC1918, CGNAT, multicast) не
    // уезжают в VPN — см. server/utils/allowed-ips.ts. NUXT_WG_SPLIT_LOCAL=false
    // возвращает классический full-tunnel 0.0.0.0/0.
    wgSplitLocal: true,
    ovpnEnabled: true,
    ovpnPort: 1194,
    ovpnProto: 'udp',
    ovpnPublicHost: '',
    logLevel: 'info',
    public: {
      panelName: 'anysda-vpn2',
      sso: {
        // NUXT_PUBLIC_SSO_ENABLED — общий рубильник (и кнопка, и авторедирект).
        enabled: false,
        // Бесшовность: неавторизованного сразу уносит в IdP, формы он не видит.
        // false — форма остаётся, но с кнопкой «Войти через Authentik».
        autoRedirect: true,
        label: 'Authentik',
      },
    },
  },

  nitro: {
    experimental: {
      tasks: true,
    },
  },

  compatibilityDate: '2025-07-16',
})
