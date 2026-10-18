import { runAccessOperations } from '../utils/access-operations'
import { isPanelReady } from '../utils/panel-ready'

export default defineNitroPlugin((nitroApp) => {
  let pending: Promise<void> | null = null
  const timer = setInterval(() => {
    if (pending || !isPanelReady()) return
    pending = runAccessOperations().catch(() => useLogger().error('access operation worker failed')).finally(() => { pending = null })
  }, 1000)
  nitroApp.hooks.hook('close', async () => { clearInterval(timer); await pending })
})
