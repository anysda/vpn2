import type { Page } from '@playwright/test'
import { expect, test } from '@playwright/test'
import { wordBreaks } from './support/words'

// Помощник wordBreaks на своей странице, без панели: перенос по слогам с дефисом браузера
// (hyphens:auto, lang="ru") допустим, разрыв посреди слова без дефиса и перенос слова,
// которое влезало в строку целиком, - нет.

// слово и ширина колонки, где браузер переносит его по слогам с дефисом
const LONG: [string, number][] = [['Выключается', 77], ['Настоящий', 59], ['Ожидающие', 50], ['Нидерланды', 60]]
const WORD = 'display:inline-block;hyphens:auto;overflow-wrap:anywhere' // как WholeWords

async function column(page: Page, width: number, html: string) {
  await page.setContent(`<html lang="ru"><body style="font:500 15px sans-serif;margin:8px">`
    + `<b id="t" style="display:block;width:${width}px">${html}</b></body></html>`)
  return page.locator('#t')
}

for (const [word, width] of LONG) {
  for (const [name, css] of [['hyphens:auto', 'hyphens:auto'], ['как WholeWords', WORD]]) {
    test(`«${word}» в колонке ${width}px, ${name}: перенос с дефисом не разрыв`, async ({ page }) => {
      const el = await column(page, width, `<span style="${css}">${word}</span>`)
      // не меньше двух строк: иначе проверять нечего
      expect(await el.evaluate(b => b.getBoundingClientRect().height / parseFloat(getComputedStyle(b).fontSize)), 'слово перенесено').toBeGreaterThan(2)
      expect(await el.evaluate(wordBreaks)).toEqual([])
    })
  }

  test(`«${word}» в колонке ${width}px, overflow-wrap:anywhere без hyphens: разрыв найден`, async ({ page }) => {
    const el = await column(page, width, `<span style="overflow-wrap:anywhere">${word}</span>`)
    const found = await el.evaluate(wordBreaks)
    expect(found.length, JSON.stringify(found)).toBeGreaterThan(0)
    expect(found.every(x => x.startsWith('разрыв без дефиса «')), JSON.stringify(found)).toBe(true)
  })
}

// hyphens:auto слово, которое влезает в строку, переносит целиком; мягкий дефис ломает его на месте
test('слово, которое влезало в строку целиком, перенесено с дефисом: найдено', async ({ page }) => {
  await page.setContent('<html lang="ru"><body style="font:500 15px sans-serif"><b id="w" style="white-space:nowrap">Нидерланды</b></body></html>')
  const width = Math.ceil((await page.locator('#w').boundingBox())!.width) + 2
  const el = await column(page, width, '<span style="hyphens:auto">Ах Нидер&shy;ланды</span>')
  const found = await el.evaluate(wordBreaks)
  expect(found).toEqual(['«Нидер\u00ADланды» помещается в строку, а перенесено «Ах Нидер\u00AD|ланды»'])
})
