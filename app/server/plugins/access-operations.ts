import { runAccessOperations } from '../utils/access-operations'
import { isPanelReady } from '../utils/panel-ready'

export default defineNitroPlugin((nitroApp) => {
  const timer = setInterval(() => {
    if (isPanelReady()) void runAccessOperations().catch(() => useLogger().error('access operation worker failed'))
  }, 1000)
  nitroApp.hooks.hook('close', () => { clearInterval(timer) })
})
