import type { Locator, Page, Response } from '@playwright/test'
import { downloadText, expect, PUBLIC_HOST, test, toast, uniq } from './support/fixtures'
import { addDevice, createClientUi, deviceCard, openClient, openHome } from './support/clients'

function closeBtn(dlg: Locator) {
  return dlg.getByRole('button', { name: /Close|Закрыть/ })
}

/** Модалка конфига девайса: заголовок — имя девайса, внутри — название протокола. */
function protoDialog(page: Page, device: string, proto: string) {
  return page.getByRole('dialog', { name: device }).filter({ hasText: proto })
}

async function download(page: Page, dlg: Locator, name = 'Скачать') {
  const wait = page.waitForEvent('download')
  await dlg.getByRole('button', { name, exact: true }).click()
  const d = await wait
  return { file: d.suggestedFilename(), text: await downloadText(d) }
}

function field(conf: string, key: string): string | undefined {
  return new RegExp(`^${key}\\s*=\\s*(.+)$`, 'm').exec(conf)?.[1]?.trim()
}

function block(conf: string, tag: string): string | undefined {
  return new RegExp(`<${tag}>\\n([\\s\\S]+?)\\n</${tag}>`).exec(conf)?.[1]
}

/** Все ответы QR-картинки WireGuard с телами — для проверки «QR перезапросился». */
function qrResponses(page: Page) {
  const got: { url: string, body: string }[] = []
  page.on('response', async (r: Response) => {
    if (!/\/wg-qrcode\.svg/.test(r.url()) || r.status() !== 200) return
    got.push({ url: r.url(), body: await r.text().catch(() => '') })
  })
  return got
}

async function qrLoaded(dlg: Locator) {
  const img = dlg.locator('img[alt="WireGuard QR"]')
  await expect(img).toBeVisible()
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0)
}

test.describe('девайсы', () => {
  test('WG: QR, .conf, перевыпуск меняет ключ и QR, удаление', async ({ page }) => {
    const qrs = qrResponses(page)
    await openHome(page)
    const client = uniq('wg')
    const device = 'e2e-phone'
    await createClientUi(page, client)
    await openClient(page, client)

    // «Отмена» в «Новый девайс».
    await page.getByRole('button', { name: 'девайс', exact: true }).click()
    const add = page.getByRole('dialog', { name: 'Новый девайс' })
    await add.getByPlaceholder('напр. iPhone').fill('lost')
    await add.getByRole('button', { name: 'Отмена' }).click()
    await expect(add).toBeHidden()
    await expect(page.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()

    await addDevice(page, device)
    const dc = deviceCard(page, device)

    await dc.wg.click()
    const wg = protoDialog(page, device, 'WireGuard')
    await qrLoaded(wg)
    const first = await download(page, wg)
    expect(first.file).toMatch(/\.conf$/)
    expect(first.text).toContain('[Interface]')
    expect(first.text).toContain('[Peer]')
    const key1 = field(first.text, 'PrivateKey')
    expect(key1).toMatch(/^[A-Za-z0-9+/]{43}=$/)
    expect(field(first.text, 'Address')).toMatch(/^\d+\.\d+\.\d+\.\d+\/\d+/)
    expect(field(first.text, 'PublicKey')).toMatch(/^[A-Za-z0-9+/]{43}=$/)
    expect(field(first.text, 'Endpoint')).toMatch(new RegExp(`^${PUBLIC_HOST.replace(/\./g, '\\.')}:\\d+$`))
    expect(qrs.length).toBeGreaterThan(0)
    const qr1 = qrs.at(-1)!.body
    expect(qr1).toContain('<svg')
    await closeBtn(wg).click()
    await expect(wg).toBeHidden()

    // Перевыпуск: «Отмена» не трогает ключ, «Перевыпустить» меняет.
    await dc.reissue.click()
    const confirm = page.getByRole('dialog', { name: 'Перевыпустить ключи девайса?' })
    await confirm.getByRole('button', { name: 'Отмена' }).click()
    await expect(confirm).toBeHidden()
    await dc.wg.click()
    expect(field((await download(page, wg)).text, 'PrivateKey')).toBe(key1)
    await closeBtn(wg).click()

    await dc.reissue.click()
    await confirm.getByRole('button', { name: 'Перевыпустить' }).click()
    await expect(toast(page, `Ключи девайса «${device}» перевыпущены`)).toBeVisible()
    await expect(confirm).toBeHidden()

    const before = qrs.length
    await dc.wg.click()
    await qrLoaded(wg)
    const second = await download(page, wg)
    const key2 = field(second.text, 'PrivateKey')
    expect(key2).toMatch(/^[A-Za-z0-9+/]{43}=$/)
    expect(key2).not.toBe(key1)
    // QR в модалке должен показывать новый конфиг, а не картинку из кэша.
    await expect.poll(() => qrs.length, { message: 'QR не перезапрошен после перевыпуска' }).toBeGreaterThan(before)
    expect(qrs.at(-1)!.body).not.toBe(qr1)
    await page.keyboard.press('Escape')
    await expect(wg).toBeHidden()

    // Удаление: «Отмена», потом всерьёз.
    await dc.remove.click()
    const del = page.getByRole('dialog', { name: 'Удалить девайс?' })
    await del.getByRole('button', { name: 'Отмена' }).click()
    await expect(del).toBeHidden()
    await expect(dc.card).toBeVisible()
    await dc.remove.click()
    await del.getByRole('button', { name: 'Удалить' }).click()
    await expect(toast(page, `Девайс «${device}» удалён`)).toBeVisible()
    await expect(dc.card).toHaveCount(0)
    await expect(page.getByText('Пока нет девайсов — добавь первый.')).toBeVisible()
  })

  test('OpenVPN: .ovpn с сертификатами, перевыпуск меняет сертификат', async ({ page }) => {
    await openHome(page)
    const client = uniq('ovpn')
    const device = 'e2e-laptop'
    await createClientUi(page, client)
    await openClient(page, client)
    await addDevice(page, device)
    const dc = deviceCard(page, device)

    await dc.ovpn.click()
    const ovpn = protoDialog(page, device, 'OpenVPN')
    await expect(ovpn.getByText('генерирую сертификат…')).toBeHidden({ timeout: 30_000 })
    const first = await download(page, ovpn)
    expect(first.file).toMatch(/\.ovpn$/)
    expect(first.text).toMatch(new RegExp(`^remote ${PUBLIC_HOST.replace(/\./g, '\\.')} \\d+$`, 'm'))
    for (const tag of ['ca', 'cert', 'key']) expect(block(first.text, tag), tag).toMatch(/^-----BEGIN [A-Z ]+-----/)
    expect(block(first.text, 'tls-crypt')).toContain('BEGIN OpenVPN Static key V1')
    const cert1 = block(first.text, 'cert')
    await closeBtn(ovpn).click()
    await expect(ovpn).toBeHidden()

    await dc.reissue.click()
    await page.getByRole('dialog', { name: 'Перевыпустить ключи девайса?' }).getByRole('button', { name: 'Перевыпустить' }).click()
    await expect(toast(page, `Ключи девайса «${device}» перевыпущены`)).toBeVisible()

    await dc.ovpn.click()
    await expect(ovpn.getByText('генерирую сертификат…')).toBeHidden({ timeout: 30_000 })
    const second = await download(page, ovpn)
    expect(block(second.text, 'cert')).toMatch(/^-----BEGIN CERTIFICATE-----/)
    expect(block(second.text, 'cert')).not.toBe(cert1)
    await closeBtn(ovpn).click()
    await expect(ovpn).toBeHidden()
  })

  test('IKEv2: сервер и логин, пароль скрыт и показывается, CA скачивается', async ({ page }) => {
    await openHome(page)
    const client = uniq('ikev2')
    const device = 'e2e-mac'
    await createClientUi(page, client)
    await openClient(page, client)
    await addDevice(page, device)
    const dc = deviceCard(page, device)

    await dc.ikev2.click()
    const ike = protoDialog(page, device, 'IKEv2')
    await expect(ike.getByText('Пароль:')).toBeVisible()
    const values = ike.locator('div.grid > span.font-mono')
    await expect(values.nth(0)).toHaveText(PUBLIC_HOST)
    await expect(values.nth(1)).not.toBeEmpty()
    await expect(values.nth(2)).toHaveText('•'.repeat(20))

    const eye = ike.locator('div.grid > div.flex').nth(2).getByRole('button').first()
    await eye.click()
    const shown = (await values.nth(2).textContent())!.trim()
    expect(shown).not.toContain('•')
    expect(shown.length).toBeGreaterThanOrEqual(12)
    await eye.click()
    await expect(values.nth(2)).toHaveText('•'.repeat(20))

    // Self-signed режим: есть «Скачать CA», файл — PEM-сертификат.
    const ca = ike.getByRole('link', { name: 'Скачать CA' }).or(ike.getByRole('button', { name: 'Скачать CA' }))
    await expect(ca).toBeVisible()
    const href = await ca.getAttribute('href')
    expect(href).toBe('/api/ikev2/ca.crt')
    const r = await page.request.get(href!)
    expect(r.status()).toBe(200)
    expect(await r.text()).toContain('-----BEGIN CERTIFICATE-----')

    await closeBtn(ike).click()
    await expect(ike).toBeHidden()
  })
})
