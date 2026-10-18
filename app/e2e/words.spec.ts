import type { Page } from '@playwright/test'
import { expect, test } from '@playwright/test'
import { wordBreaks } from './support/words'

// Помощник wordBreaks на своей странице, без панели: слово не делится никогда - ни переносом
// по слогам с дефисом браузера (hyphens:auto, lang="ru", мягкий дефис), ни без дефиса.
// Строка начинается только с начала слова.

// слово и ширина колонки, где браузер переносит его по слогам с дефисом
const LONG: [string, number][] = [['Выключается', 77], ['Настоящий', 59], ['Ожидающие', 50], ['Нидерланды', 60]]

async function column(page: Page, width: number, html: string) {
  await page.setContent(`<html lang="ru"><body style="font:500 15px sans-serif;margin:8px">`
    + `<b id="t" style="display:block;width:${width}px">${html}</b></body></html>`)
  return page.locator('#t')
}

for (const [word, width] of LONG) {
  for (const css of ['hyphens:auto', 'overflow-wrap:anywhere', 'hyphens:auto;overflow-wrap:anywhere', 'word-break:break-all']) {
    test(`«${word}» в колонке ${width}px, ${css}: разрыв найден`, async ({ page }) => {
      const el = await column(page, width, `<span style="${css}">${word}</span>`)
      // не меньше двух строк: иначе проверять нечего
      expect(await el.evaluate(b => b.getBoundingClientRect().height / parseFloat(getComputedStyle(b).fontSize)), 'слово перенесено').toBeGreaterThan(2)
      const found = await el.evaluate(wordBreaks)
      expect(found.length, JSON.stringify(found)).toBeGreaterThan(0)
      expect(found.every(x => x.startsWith('разрыв посреди слова «')), JSON.stringify(found)).toBe(true)
    })
  }

  test(`«${word}» шире колонки ${width}px без правил переноса: не рвётся, вылезание найдено`, async ({ page }) => {
    const el = await column(page, width, word)
    const found = await el.evaluate(wordBreaks)
    expect(found.length, JSON.stringify(found)).toBeGreaterThan(0)
    expect(found.every(x => / за краем на \d+px$/.test(x)), JSON.stringify(found)).toBe(true)
  })
}

test('слова переносятся целиком по пробелам и после дефиса в тексте: разрывов нет', async ({ page }) => {
  await page.setContent('<html lang="ru"><body style="font:500 15px sans-serif"><b id="w" style="white-space:nowrap">Нидерланды</b></body></html>')
  const width = Math.ceil((await page.locator('#w').boundingBox())!.width) + 2
  const el = await column(page, width, 'Ах Нидерланды и IPv6-адреса')
  expect(await el.evaluate(b => b.getBoundingClientRect().height / parseFloat(getComputedStyle(b).fontSize)), 'текст в несколько строк').toBeGreaterThan(2)
  expect(await el.evaluate(wordBreaks)).toEqual([])
})

test('мягкий дефис ломает слово, которое влезало в строку: найдено', async ({ page }) => {
  await page.setContent('<html lang="ru"><body style="font:500 15px sans-serif"><b id="w" style="white-space:nowrap">Нидерланды</b></body></html>')
  const width = Math.ceil((await page.locator('#w').boundingBox())!.width) + 2
  const el = await column(page, width, 'Ах Нидер&shy;ланды')
  expect(await el.evaluate(wordBreaks)).toEqual(['разрыв посреди слова «Ах Нидер­|ланды»'])
})
