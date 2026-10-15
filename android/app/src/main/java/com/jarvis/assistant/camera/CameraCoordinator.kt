package com.jarvis.assistant.camera

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.activity.ComponentActivity
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.FileProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File

sealed interface CaptureResult {
    data class Saved(val uri: Uri, val bytes: Long) : CaptureResult
    data object Cancelled : CaptureResult
    data object PermissionDenied : CaptureResult
    data class Failed(val reason: String) : CaptureResult
}

/**
 * Takes a photo only when asked (user command or approved plan step), after the CAMERA runtime permission.
 *
 * Lives in the app container, so a pending capture survives Activity recreation (rotation): the new
 * Activity re-registers the launchers in onCreate and receives the result. A capture counts as saved only
 * if the camera app actually wrote a non-empty image; cancelled shots are deleted.
 */
class CameraCoordinator(private val context: Context) {
    private var activity: ComponentActivity? = null
    private var permissionLauncher: ActivityResultLauncher<String>? = null
    private var captureLauncher: ActivityResultLauncher<Uri>? = null
    private var pendingPermission: CompletableDeferred<Boolean>? = null
    private var pendingCapture: CompletableDeferred<Boolean>? = null
    private val mutex = Mutex()

    /** Call from Activity.onCreate. */
    fun attach(activity: ComponentActivity) {
        this.activity = activity
        permissionLauncher = activity.registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            pendingPermission?.complete(granted)
        }
        captureLauncher = activity.registerForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
            pendingCapture?.complete(ok)
        }
    }

    /** Call from Activity.onDestroy. Pending results are kept for the recreated Activity. */
    fun detach(activity: ComponentActivity) {
        if (this.activity === activity) {
            this.activity = null
            permissionLauncher = null
            captureLauncher = null
        }
    }

    fun hasPermission(): Boolean =
        context.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    suspend fun capture(): CaptureResult {
        mutex.withLock {
            return captureLocked()
        }
    }

    private suspend fun captureLocked(): CaptureResult {
        if (!hasPermission()) {
            val launcher = permissionLauncher ?: return CaptureResult.Failed("JARVIS isn't on screen")
            val granted = CompletableDeferred<Boolean>().also { pendingPermission = it }
            launcher.launch(Manifest.permission.CAMERA)
            if (!granted.await()) return CaptureResult.PermissionDenied
        }
        val target = PhotoTarget.create(context) ?: return CaptureResult.Failed("Couldn't create a photo file")
        val launcher = captureLauncher ?: run {
            target.discard()
            return CaptureResult.Failed("JARVIS isn't on screen")
        }
        val taken = CompletableDeferred<Boolean>().also { pendingCapture = it }
        try {
            launcher.launch(target.uri)
        } catch (e: ActivityNotFoundException) {
            target.discard()
            return CaptureResult.Failed("No camera app is installed")
        }
        val ok = taken.await()
        val bytes = target.size()
        return when {
            ok && bytes > 0 -> {
                target.publish()
                CaptureResult.Saved(target.uri, bytes)
            }
            ok -> { // camera said OK but wrote nothing: a failure, not the user's cancel
                target.discard()
                CaptureResult.Failed("The camera app didn't save a photo")
            }
            else -> {
                target.discard()
                CaptureResult.Cancelled
            }
        }
    }
}

/** Where the camera app writes the photo: Pictures/JARVIS via MediaStore (API 29+), app-specific folder below. */
private class PhotoTarget(
    private val context: Context,
    val uri: Uri,
    private val file: File?,
) {
    fun size(): Long = if (file != null) file.length() else
        runCatching { context.contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize } ?: 0L }.getOrDefault(0L)

    fun publish() {
        if (file == null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            context.contentResolver.update(uri, ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }, null, null)
        }
    }

    fun discard() {
        if (file != null) file.delete() else runCatching { context.contentResolver.delete(uri, null, null) }
    }

    companion object {
        fun create(context: Context): PhotoTarget? = runCatching {
            val name = "JARVIS_${System.currentTimeMillis()}.jpg"
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, name)
                    put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                    put(MediaStore.Images.Media.RELATIVE_PATH, "${Environment.DIRECTORY_PICTURES}/JARVIS")
                    put(MediaStore.Images.Media.IS_PENDING, 1)
                }
                val uri = context.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
                uri?.let { PhotoTarget(context, it, null) }
            } else {
                val dir = File(context.getExternalFilesDir(Environment.DIRECTORY_PICTURES), "JARVIS").apply { mkdirs() }
                val file = File(dir, name)
                PhotoTarget(context, FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file), file)
            }
        }.getOrNull()
    }
}
