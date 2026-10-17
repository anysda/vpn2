/** Окно до 720px: мобильный каркас (main.css, @media (max-width:720px)). До монтирования — десктоп. */
export function useMobile() {
  const on = useState('mobile', () => false)
  let mq: MediaQueryList | undefined
  const set = () => { on.value = !!mq?.matches }
  onMounted(() => {
    mq = matchMedia('(max-width: 720px)')
    set()
    mq.addEventListener('change', set)
  })
  onUnmounted(() => mq?.removeEventListener('change', set))
  return on
}
