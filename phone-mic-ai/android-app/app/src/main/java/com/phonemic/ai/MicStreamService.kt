package com.phonemic.ai

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.phonemic.ai.transport.BluetoothTransport
import com.phonemic.ai.transport.Transport
import com.phonemic.ai.transport.UsbTransport
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.IOException
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * Foreground service: records the mic and streams it to the laptop, so it
 * keeps working with the screen off. Reconnects automatically every 2 s.
 */
class MicStreamService : Service() {

    data class State(val running: Boolean = false, val status: String = "Stopped", val levelDb: Float = -90f)

    companion object {
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val EXTRA_MODE = "mode"            // "bluetooth" | "usb"
        const val EXTRA_ADDRESS = "address"      // laptop MAC for Bluetooth
        private const val CHANNEL_ID = "mic_stream"
        private const val NOTIF_ID = 1
        private const val RETRY_MS = 2000L

        private val _state = MutableStateFlow(State())
        val state: StateFlow<State> = _state
    }

    @Volatile private var running = false
    private var worker: Thread? = null
    private var transport: Transport? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopStreaming(); stopSelf(); return START_NOT_STICKY }
            ACTION_START -> {
                val mode = intent.getStringExtra(EXTRA_MODE) ?: "bluetooth"
                val address = intent.getStringExtra(EXTRA_ADDRESS)
                startInForeground()
                startStreaming(mode, address)
            }
        }
        return START_NOT_STICKY
    }

    private fun startInForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ID, "Microphone stream", NotificationManager.IMPORTANCE_LOW))
        }
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        ServiceCompat.startForeground(this, NOTIF_ID, notification("Starting..."), type)
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, MicStreamService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Phone Mic AI")
            .setContentText(text)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stop)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun publish(status: String, level: Float = _state.value.levelDb) {
        _state.value = State(running, status, level)
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification(status))
    }

    private fun startStreaming(mode: String, address: String?) {
        if (running) return
        running = true
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PhoneMicAI:stream").apply { acquire() }

        transport = if (mode == "usb") UsbTransport(StreamProtocol.USB_PORT) else {
            val adapter = getSystemService(BluetoothManager::class.java)?.adapter
            if (adapter == null || address == null) {
                running = false
                publish("No Bluetooth adapter / laptop selected")
                stopStreaming(); stopSelf()
                return
            }
            BluetoothTransport(adapter, address)
        }
        worker = Thread({ streamLoop() }, "mic-stream").apply { start() }
    }

    private fun streamLoop() {
        val capture = AudioCapture()
        val t = transport ?: return
        try {
            capture.start()
        } catch (e: Exception) {
            publish("Microphone error: ${e.message}")
            running = false
            return
        }
        val buf = ByteArray(capture.chunkBytes)
        while (running) {
            publish(if (t is UsbTransport) "USB: waiting for the laptop (adb forward tcp:5000 tcp:5000)..." else "Bluetooth: connecting to the laptop...")
            try {
                val out = t.open()
                out.write(StreamProtocol.header())
                out.flush()
                publish("Streaming over ${t.label} · 16 kHz mono")
                var lastUi = 0L
                while (running) {
                    val n = capture.read(buf)
                    if (n <= 0) throw IOException("microphone read failed ($n)")
                    out.write(buf, 0, n)
                    val now = System.currentTimeMillis()
                    if (now - lastUi > 250) { lastUi = now; _state.value = _state.value.copy(levelDb = levelDb(buf, n)) }
                }
            } catch (e: IOException) {
                if (!running) break
                publish("${t.label} disconnected: ${e.message ?: "link lost"}. Retrying...", -90f)
                if (t is UsbTransport) t.closeClient() else t.close()
                try { Thread.sleep(RETRY_MS) } catch (_: InterruptedException) { break }
            }
        }
        capture.stop()
        t.close()
    }

    private fun levelDb(buf: ByteArray, n: Int): Float {
        var sum = 0.0
        var i = 0
        while (i + 1 < n) {
            val s = ((buf[i + 1].toInt() shl 8) or (buf[i].toInt() and 0xFF)).toShort() / 32768.0
            sum += s * s
            i += 2
        }
        val rms = sqrt(sum / maxOf(1, n / 2))
        return (20 * log10(maxOf(rms, 1e-9))).toFloat()
    }

    private fun stopStreaming() {
        running = false
        transport?.close()                    // unblocks connect()/accept()/write()
        worker?.interrupt()
        worker = null
        transport = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        _state.value = State(false, "Stopped")
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
    }

    override fun onDestroy() {
        stopStreaming()
        super.onDestroy()
    }
}

/** Convenience used by MainActivity. */
fun Context.startMicService(mode: String, address: String?) {
    val i = Intent(this, MicStreamService::class.java)
        .setAction(MicStreamService.ACTION_START)
        .putExtra(MicStreamService.EXTRA_MODE, mode)
        .putExtra(MicStreamService.EXTRA_ADDRESS, address)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(i) else startService(i)
}

fun Context.stopMicService() {
    startService(Intent(this, MicStreamService::class.java).setAction(MicStreamService.ACTION_STOP))
}
