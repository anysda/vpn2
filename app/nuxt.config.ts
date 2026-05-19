export default defineNuxtConfig({
  devtools: { enabled: true },

  modules: [
    '@nuxt/ui',
    '@nuxt/eslint',
    'nuxt-auth-utils',
  ],

  css: ['~/assets/css/main.css'],

  runtimeConfig: {
    databaseUrl: 'file:./local.db',
    ssConfigPath: '/etc/outline-ss-server/config.yml',
    ssPort: 443,
    ssCipher: 'chacha20-ietf-poly1305',
    ssPublicHost: '',
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
    tgbotEventPort: 8877,
    tgbotSecret: '',
    logLevel: 'info',
    public: {
      panelName: 'anysda-vpn2',
    },
  },

  nitro: {
    experimental: {
      tasks: true,
    },
  },

  compatibilityDate: '2025-07-16',
})
