package com.phonemic.ai.transport

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothSocket
import com.phonemic.ai.StreamProtocol
import java.io.IOException
import java.io.OutputStream

/**
 * Bluetooth RFCOMM client. The laptop (already paired) advertises
 * StreamProtocol.SERVICE_UUID; we look it up via SDP and connect.
 * No Wi-Fi or internet is involved.
 */
class BluetoothTransport(
    private val adapter: BluetoothAdapter,
    private val laptopAddress: String,
) : Transport {

    @Volatile private var socket: BluetoothSocket? = null
    override val label = "Bluetooth"

    @SuppressLint("MissingPermission") // BLUETOOTH_CONNECT is checked in MainActivity
    override fun open(): OutputStream {
        if (!adapter.isEnabled) throw IOException("Bluetooth is off")
        val device = adapter.getRemoteDevice(laptopAddress)
        adapter.cancelDiscovery()            // discovery slows down / breaks connects
        val s = device.createRfcommSocketToServiceRecord(StreamProtocol.SERVICE_UUID)
        socket = s
        try {
            s.connect()                      // blocks; throws if the PC server isn't running
        } catch (e: IOException) {
            close()
            throw IOException("Laptop not reachable over Bluetooth (is main.py running?)", e)
        }
        return s.outputStream
    }

    override fun close() {
        try { socket?.close() } catch (_: IOException) {}
        socket = null
    }
}
