import { ADMIN_USER, expect, hydrated, loginUi, test } from './support/fixtures'

test('засев без секрета оставляет форму и парольный вход', async ({ page }) => {
  test.skip(!process.env.E2E_FAULT_CONTAINER, 'нужен локальный засев из run-local.sh')
  expect(await (await page.request.get('/api/sso')).json()).toMatchObject({ enabled: false, autoRedirect: false })
  const external: string[] = []
  page.on('request', (request) => {
    if (new URL(request.url()).hostname === 'auth.example.test') external.push(request.url())
  })
  await page.goto('/')
  await hydrated(page)
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByLabel('Пароль', { exact: true })).toBeVisible()
  await loginUi(page)
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('button', { name: `@${ADMIN_USER}` })).toBeVisible()
  expect(external).toEqual([])
})
