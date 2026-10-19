import { expect, test, uniq } from './support/fixtures'
import { clientCard, clientName, createClientUi, openClient, openHome } from './support/clients'

for (const width of [1400, 390]) {
  test.describe(`переключение клиента, ${width}px`, () => {
    test.use({ viewport: { width, height: 844 } })

    test('запоздавший GET прежнего клиента не подменяет выбранного', async ({ page }) => {
      await openHome(page, '/clients')
      const first = uniq('first')
      const second = uniq('second')
      await createClientUi(page, first)
      await createClientUi(page, second)
      const clients: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
      const id = clients.find(c => c.name === first)!.id
      let release!: () => void
      const held = new Promise<void>((resolve) => { release = resolve })
      let fetched!: () => void
      const ready = new Promise<void>((resolve) => { fetched = resolve })
      await page.route(`**/api/clients/${id}`, async (route) => {
        const response = await route.fetch()
        fetched()
        await held
        await route.fulfill({ response })
      })
      const panel = page.getByTestId('client-detail')
      try {
        await page.getByPlaceholder('Поиск...').fill(first)
        await clientCard(page, first).click()
        await ready
        // До 720px карточка — отдельный экран: к списку возвращает «Назад».
        if (width <= 720) await page.getByRole('button', { name: 'Назад' }).click()
        await openClient(page, second)
      }
      finally {
        release()
      }
      await page.unrouteAll({ behavior: 'wait' })
      // Дать браузеру обработать освобождённый ответ и следующую отрисовку.
      await page.waitForLoadState('networkidle')
      await expect(clientName(panel)).toHaveText(second)
      await expect(clientCard(page, second)).toHaveAttribute('aria-pressed', 'true')
    })
  })
}

test.describe('переключение клиента, ввод имени до загрузки нового', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('VPN2-524: пока грузится карточка B, имя клиента A не правится и не сохраняется', async ({ page }) => {
    await openHome(page, '/clients')
    const a = uniq('a')
    const b = uniq('b')
    await createClientUi(page, a)
    await createClientUi(page, b)
    const clients: { id: number, name: string }[] = await (await page.request.get('/api/clients')).json()
    const idA = clients.find(c => c.name === a)!.id
    const idB = clients.find(c => c.name === b)!.id
    const panel = await openClient(page, a)
    const patches: string[] = []
    page.on('request', (r) => { if (r.method() === 'PATCH') patches.push(r.url()) })
    let release!: () => void
    const held = new Promise<void>((resolve) => { release = resolve })
    let fetched!: () => void
    const ready = new Promise<void>((resolve) => { fetched = resolve })
    await page.route(`**/api/clients/${idB}`, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      const response = await route.fetch()
      fetched()
      await held
      await route.fulfill({ response })
    })
    try {
      await page.getByPlaceholder('Поиск...').fill(b)
      await clientCard(page, b).click()
      await ready
      // Карточка B ещё не пришла: на экране A. Человек ставит курсор в имя и вводит новое.
      await expect(clientName(panel)).toHaveText(a)
      await clientName(panel).focus()
      await page.keyboard.type('-renamed')
      await page.keyboard.press('Enter')
    }
    finally {
      release()
    }
    await page.unrouteAll({ behavior: 'wait' })
    await expect(clientName(panel)).toHaveText(b)
    await page.waitForLoadState('networkidle')
    expect(patches.filter(u => new RegExp(`/api/clients/${idA}$`).test(u))).toEqual([])
    const after: { id: number, name: string } = await (await page.request.get(`/api/clients/${idA}`)).json()
    expect(after.name).toBe(a)
  })
})
