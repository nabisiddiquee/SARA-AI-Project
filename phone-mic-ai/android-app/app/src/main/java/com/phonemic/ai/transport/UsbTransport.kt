package com.phonemic.ai.transport

import java.io.IOException
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket

/**
 * USB mode: listen on the phone's own 127.0.0.1:port. On the laptop,
 * `adb forward tcp:5000 tcp:5000` tunnels the USB cable to this socket.
 * Only loopback is used, so no Wi-Fi / mobile data is needed.
 */
class UsbTransport(private val port: Int) : Transport {

    @Volatile private var server: ServerSocket? = null
    @Volatile private var client: Socket? = null
    override val label = "USB"

    override fun open(): OutputStream {
        val srv = server ?: ServerSocket(port, 1, InetAddress.getByName("127.0.0.1")).also {
            it.reuseAddress = true
            server = it
        }
        val s = srv.accept()                 // blocks until the laptop connects
        s.tcpNoDelay = true
        s.sendBufferSize = 16 * 1024
        client = s
        return s.getOutputStream()
    }

    /** Drops the current laptop connection but keeps listening. */
    fun closeClient() {
        try { client?.close() } catch (_: IOException) {}
        client = null
    }

    override fun close() {
        closeClient()
        try { server?.close() } catch (_: IOException) {}
        server = null
    }
}
