import { loginApi, test } from './support/fixtures'

/**
 * Обработчик page.route с route.fetch() ещё работает, когда тест кончился:
 * опрос карточки клиента ушёл перед закрытием страницы. Контекст закрывается,
 * ответ route.fetch() освобождён, json() бросает «Response has been disposed».
 * Это не провал теста: перехват снимается в конце теста без ожидания.
 */
test('обработчик route, застигнутый концом теста, не валит тест', async ({ page }) => {
  await loginApi(page)
  await page.goto('/')
  let fetched!: () => void
  const inside = new Promise<void>(resolve => fetched = resolve)
  const closed = new Promise(resolve => page.context().once('close', resolve))
  await page.route(/\/api\/clients\?teardown$/, async (route) => {
    const response = await route.fetch()
    fetched()
    await closed
    await route.fulfill({ response, json: await response.json() })
  })
  await page.evaluate(() => {
    void fetch('/api/clients?teardown').catch(() => {})
  })
  await inside
})
