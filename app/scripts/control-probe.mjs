import { createConnection } from 'node:net'

// Copied into each image: deploy checks that image's requirements before stop.
const required = ['wg.apply', 'openvpn.apply', 'ikev2.apply', 'state.read',
  'profile.read', 'traffic.read', 'openvpn.issue', 'openvpn.revoke',
  'openvpn.disconnect', 'openvpn.certificate', 'settings.read', 'settings.apply', 'routes.plan', 'expiry.run']
const socket = createConnection('/run/vpn2-control/control.sock')
const timer = setTimeout(() => { socket.destroy(); process.exitCode = 1 }, 5000)
let data = ''
socket.on('connect', () => socket.write(JSON.stringify({ api: 1, method: 'api.describe', params: {} }) + '\n'))
socket.on('error', () => { clearTimeout(timer); process.exitCode = 1 })
socket.on('data', chunk => {
  data += chunk
  if (data.length > 65536) { socket.destroy(); clearTimeout(timer); process.exitCode = 1; return }
  if (!data.includes('\n')) return
  clearTimeout(timer)
  socket.destroy()
  try {
    const reply = JSON.parse(data)
    if (reply.api !== 1 || !reply.ok || !required.every(method => reply.result.methods.includes(method))) throw new Error('incompatible')
    console.log('panel: control API 1 compatible; dedicated UID authorized')
  }
  catch { process.exitCode = 1 }
})
