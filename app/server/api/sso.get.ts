import { ssoSettings } from '../utils/sso'

/** Что нужно странице входа до входа: есть ли кнопка SSO и уводить ли сразу. */
export default defineEventHandler(() => {
  const s = ssoSettings()
  return { enabled: s.enabled, autoRedirect: s.autoRedirect, label: s.label }
})
