import { ssoConfigured, ssoSettings } from '../utils/sso'

/** Что нужно странице входа до входа: есть ли кнопка SSO и уводить ли сразу. */
export default defineEventHandler(() => {
  const s = ssoSettings()
  return { enabled: s.enabled && ssoConfigured(s), autoRedirect: s.autoRedirect, label: s.label }
})
