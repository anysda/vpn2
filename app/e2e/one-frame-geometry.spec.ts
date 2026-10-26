import type { Page } from '@playwright/test'
import { openHome } from './support/clients'
import { expect, test } from './support/fixtures'
import { rects } from './support/geometry'

// Каскад появления сдвигает карточки по одной с разной задержкой: два
// boundingBox() подряд берут их из разных кадров. rects() снимает все
// прямоугольники за один кадр — зазор рассылки под списком всегда 12px.
const LIST = '.ccol .clist'
const CARD = '.ov-broadcast'
const GAP = 12

type Rect = { x: number, y: number, width: number, height: number }

function gap([list, card]: Rect[]) {
  return card!.y - (list!.y + list!.height)
}

interface Frozen {
  /** Все анимации страницы: на паузе в начале, время двигает только тест. */
  __anims: Animation[]
  /** Элементы, измеренные getBoundingClientRect(), по задачам JS: одна задача — один кадр. */
  __frames: Element[][]
  /** Сдвигать ли каскад в конец после каждой задачи, где был замер. */
  __shift: boolean
}

/** Конец каскада: позже последней задержки с длительностью (60 + 8 * 55 + 520). */
const END = 2000

/**
 * Каждая анимация страницы с рождения стоит на паузе в начале и лежит в
 * window.__anims: каскад не доигрывает и не пропадает из getAnimations(),
 * сколько бы ни шёл тест. getBoundingClientRect() записывает, какие элементы
 * измерены в одной задаче, и при __shift по её окончании (microtask) уводит
 * каскад в конец: следующее обращение к странице видит уже другой кадр.
 */
async function openFrozen(page: Page) {
  await page.addInitScript((end) => {
    const w = window as unknown as Frozen
    w.__anims = []
    w.__frames = []
    w.__shift = false
    const animate = Element.prototype.animate
    Element.prototype.animate = function (...args: Parameters<Element['animate']>) {
      const a = animate.apply(this, args)
      a.pause()
      a.currentTime = 0
      w.__anims.push(a)
      return a
    }
    const measure = Element.prototype.getBoundingClientRect
    let batch: Element[] | null = null
    Element.prototype.getBoundingClientRect = function () {
      if (!batch) {
        batch = []
        w.__frames.push(batch)
        queueMicrotask(() => {
          batch = null
          if (!w.__shift) return
          for (const a of w.__anims) {
            if ((a.effect as KeyframeEffect | null)?.target?.matches('.card, .card-ui')) a.currentTime = end
          }
        })
      }
      batch.push(this)
      return measure.call(this)
    }
  }, END)
  await openHome(page, '/clients')
  await expect(page.locator(CARD).getByRole('heading', { name: 'Рассылка от бота', exact: true })).toBeVisible()
  // Каскад запущен и для списка, и для рассылки.
  await page.waitForFunction(sels => sels.every(s => (window as unknown as Frozen).__anims
    .some(a => (a.effect as KeyframeEffect | null)?.target === document.querySelector(s))), [LIST, CARD])
}

/** Поставить каскад в начало (обе карточки на 22px ниже) и забыть прошлые замеры. */
async function rewind(page: Page) {
  await page.evaluate(() => {
    const w = window as unknown as Frozen
    for (const a of w.__anims) {
      if ((a.effect as KeyframeEffect | null)?.target?.matches('.card, .card-ui')) a.currentTime = 0
    }
    w.__frames = []
  })
}

test.describe('геометрия из одного кадра', () => {
  test('прямоугольники по порядку селекторов, как у getBoundingClientRect', async ({ page }) => {
    await openFrozen(page)

    const got = await rects(page, LIST, CARD)
    expect(got).toHaveLength(2)
    expect(Math.abs(gap(got) - GAP)).toBeLessThanOrEqual(0.5)

    const ref = await page.evaluate(sels => sels.map((s) => {
      const r = document.querySelector(s)!.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    }), [LIST, CARD])
    for (const [i, r] of ref.entries()) {
      for (const k of ['x', 'y', 'width', 'height'] as const) {
        expect(Math.abs(got[i]![k] - r[k]), `${[LIST, CARD][i]} ${k}`).toBeLessThanOrEqual(0.5)
      }
    }
  })

  test('один кадр: между обращениями к странице каскад сдвигается, зазор ровно 12px', async ({ page }) => {
    await openFrozen(page)
    // Зубы проверки: начало и конец каскада различаются для списка на 22px,
    // а рассылка под ним в обоих кадрах стоит на 12px ниже.
    const shift = await page.evaluate((end) => {
      const w = window as unknown as Frozen
      const [list, card] = ['.ccol .clist', '.ov-broadcast'].map(s => document.querySelector(s)!)
      const cascade = w.__anims.filter(a => (a.effect as KeyframeEffect | null)?.target?.matches('.card, .card-ui'))
      const at = (t: number) => {
        for (const a of cascade) a.currentTime = t
        const l = list!.getBoundingClientRect()
        return { y: l.y, gap: card!.getBoundingClientRect().y - l.bottom }
      }
      const [a, b] = [at(0), at(end)]
      return { dy: a.y - b.y, gaps: [a.gap, b.gap] }
    }, END)
    expect(Math.abs(shift.dy - 22)).toBeLessThanOrEqual(0.5)
    for (const g of shift.gaps) expect(Math.abs(g - GAP)).toBeLessThanOrEqual(0.5)

    await page.evaluate(() => { (window as unknown as Frozen).__shift = true })
    const seen: string[] = []
    for (let i = 0; i < 10; i++) {
      await rewind(page)
      const got = await rects(page, LIST, CARD)
      // Оба прямоугольника сняты одним getBoundingClientRect-проходом в одной
      // задаче JS. Замер вне страницы (CDP) или по задаче на селектор сюда не попадёт.
      const oneTask = await page.evaluate(sels => (window as unknown as Frozen).__frames
        .some(b => sels.every(s => b.includes(document.querySelector(s)!))), [LIST, CARD])
      seen.push(`${gap(got).toFixed(2)}${oneTask ? '' : ' (не одна задача)'}`)
    }
    const report = `зазоры по замерам: ${seen.join(', ')}`
    expect(seen.filter(v => v.includes('не одна задача')), report).toEqual([])
    for (const v of seen) expect(Math.abs(Number.parseFloat(v) - GAP), report).toBeLessThanOrEqual(0.5)
  })

  test('нет элемента: ошибка с именем селектора', async ({ page }) => {
    await openHome(page, '/clients')
    await expect(page.locator(CARD).getByRole('heading', { name: 'Рассылка от бота', exact: true })).toBeVisible()
    await expect(rects(page, LIST, '.нет-такого')).rejects.toThrow('.нет-такого')
  })
})
