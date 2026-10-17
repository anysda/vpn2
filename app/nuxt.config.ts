export default defineNuxtConfig({
  devtools: { enabled: true },

  modules: [
    '@nuxt/ui',
    '@nuxt/eslint',
  ],

  css: ['~/assets/css/main.css'],

  // viewport-fit=cover: мобильная шапка и таб-бар сами отступают от выреза
  // и полоски «домой» через env(safe-area-inset-*).
  app: {
    head: {
      viewport: 'width=device-width, initial-scale=1, viewport-fit=cover',
    },
  },

  // Manrope берётся из Google Fonts. Неиспользуемые каталоги
  // Fontsource/Fontshare задерживали сборку на ~68 с при их отказе.
  fonts: {
    provider: 'google',
    defaults: {
      weights: [200, 300, 400, 500, 600],
      subsets: ['cyrillic', 'latin'],
    },
  },

  // Тема по умолчанию как в системе; data-theme нужен токенам в main.css.
  colorMode: {
    preference: 'system',
    fallback: 'dark',
    dataValue: 'theme',
  },

  runtimeConfig: {
    // Сессии панели (better-auth, server/utils/auth.ts).
    session: {
      // NUXT_SESSION_PASSWORD — секрет подписи cookie и шифрования 2FA-секретов.
      password: '',
      // NUXT_SESSION_COOKIE_SECURE — флаг Secure у cookie. Стенд ходит по
      // HTTP (Caddy на :80 без TLS), поэтому по умолчанию выключен; стадия 30
      // включает его, когда у панели есть домен.
      cookie: { secure: false },
    },
    // Попыток входа (пароль или код 2FA) за 5 минут с одного адреса.
    authLoginMaxAttempts: 10,
    // --- OIDC / Authentik -----------------------------------------------------
    // Заполняется стадией 30-frontend из блока `sso:` в config.yaml:
    //   NUXT_OAUTH_OIDC_CLIENT_ID / _CLIENT_SECRET / _OPENID_CONFIG / _REDIRECT_URL
    // Это и блоки sso ниже — только засев: после сохранения в профиле панели
    // действует /etc/anysda/sso-runtime.json (server/utils/sso.ts).
    // Протокол ведёт better-auth, вход и возврат — server/routes/auth/authentik.get.ts.
    oauth: {
      oidc: {
        clientId: '',
        clientSecret: '',
        openidConfig: '',
        redirectURL: '',
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
    clientPrefsFilePath: '/etc/anysda/client-prefs.json',
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
    // Публичные адреса нод для карточек мониторинга: «ru=1.2.3.4,uz=5.6.7.8»
    // (стадия 30 собирает из config.yaml). Также узнаём переименование экзитов.
    nodeHosts: '',
    // Только для первого обновления: карта из контейнера предыдущей панели.
    previousNodeHosts: '',
    wgMtu: 1420,
    // Сплит-туннель: локальные сети клиента (RFC1918, CGNAT, multicast) не
    // уезжают в VPN — см. server/utils/allowed-ips.ts. NUXT_WG_SPLIT_LOCAL=false
    // возвращает классический full-tunnel 0.0.0.0/0.
    wgSplitLocal: true,
    ovpnEnabled: true,
    ovpnPort: 1194,
    ovpnProto: 'udp',
    ovpnPublicHost: '',
    public: {
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
