package com.phonemic.ai.transport

import java.io.Closeable
import java.io.OutputStream

/** One way of getting bytes to the laptop. [open] blocks until connected. */
interface Transport : Closeable {
    val label: String
    /** Connect (Bluetooth) or wait for the laptop (USB). Throws IOException on failure. */
    fun open(): OutputStream
}
