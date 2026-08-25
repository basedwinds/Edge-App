import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // BIND IPv4 LOOPBACK EXPLICITLY.
    //
    // Vite's default host is "localhost", which on this machine resolved to the
    // IPv6 loopback ONLY -- `netstat` showed a single listener on [::1]:5173 and
    // nothing on 127.0.0.1. So http://localhost:5173 loaded fine while
    // http://127.0.0.1:5173 was refused outright, which looks identical to "the
    // app isn't running" if that is the address in your bookmark or shortcut.
    // User-reported 2026-08-21, with both servers up and healthy at the time.
    //
    // 127.0.0.1 rather than true/0.0.0.0 on purpose: this keeps the dev server
    // on the loopback and off the LAN. Browsers fall back from ::1 to 127.0.0.1
    // for "localhost", so pinning IPv4 makes BOTH addresses work, where pinning
    // IPv6 only makes one.
    host: '127.0.0.1',
    // Vite does NOT fail when 5173 is taken -- it quietly binds 5174 (observed
    // 2026-08-17), so the launcher's "already listening" guard would pass while
    // the app actually moved. Fail loudly instead.
    strictPort: true,
  },
})
