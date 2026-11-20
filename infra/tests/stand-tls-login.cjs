// Браузер на стенде: вход в панель поверх самоподписанной пары (login) и та же сессия
// после смены сертификата (check). node stand-tls-login.cjs login|check <url> <файл сессии>;
// пароль - в PANEL_PASSWORD. Как человек после «принять риск»: ignoreHTTPSErrors.
const { chromium } = require('playwright')

const [mode, url, stateFile] = process.argv.slice(2)
const user = process.env.PANEL_USER || 'anysda'

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({ ignoreHTTPSErrors: true, ...(mode === 'check' ? { storageState: stateFile } : {}) })
  const page = await context.newPage()
  if (mode === 'login') {
    // break-glass вход формой: тот же путь, что при включённом SSO.
    await page.goto(`${url}/login?direct=1`)
    await page.waitForFunction(() => document.querySelector('#__nuxt')?.__vue_app__?.$nuxt?.isHydrating === false)
    await page.getByLabel('Логин').fill(user)
    await page.getByLabel('Пароль').fill(process.env.PANEL_PASSWORD)
    await page.getByRole('button', { name: 'Войти' }).click()
  } else {
    await page.goto(url)
  }
  await page.getByRole('button', { name: `@${user}` }).waitFor({ timeout: 30_000 })
  const session = (await context.cookies()).filter(cookie => cookie.name.includes('session_token'))
  if (!session.length || session.some(cookie => !cookie.secure)) {
    throw new Error(`cookie сессии без Secure: ${JSON.stringify(session.map(cookie => [cookie.name, cookie.secure]))}`)
  }
  if (mode === 'login') await context.storageState({ path: stateFile })
  console.log(`${mode}: в панели как @${user}, cookie ${session.map(cookie => cookie.name).join(' ')} Secure`)
  await browser.close()
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
