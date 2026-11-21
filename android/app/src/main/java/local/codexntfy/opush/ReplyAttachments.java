package local.codexntfy.opush;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageDecoder;
import android.net.Uri;
import android.os.Build;
import android.provider.OpenableColumns;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Locale;
import java.util.UUID;

final class ReplyAttachments {
    static final int MAX_FILES = 4;
    static final int MAX_BYTES = 8 * 1024 * 1024;
    private static final int IMAGE_SIDE = 2048;
    private final Context context;
    private final File directory;

    ReplyAttachments(Context context) {
        this.context = context.getApplicationContext();
        directory = new File(context.getFilesDir(), "reply_attachments");
    }

    JSONObject importAttachment(Uri uri, boolean image, boolean photo, long remaining) throws IOException, JSONException {
        ContentResolver resolver = context.getContentResolver();
        String name = photo ? "照片.jpg" : filename(resolver, uri);
        String mime = resolver.getType(uri);
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("Attachment folder unavailable");
        String id = UUID.randomUUID().toString();
        File file = file(id);
        try {
            try (FileOutputStream out = new FileOutputStream(file)) {
                if (image) {
                    saveImage(resolver, uri, out);
                    mime = "image/jpeg";
                } else {
                    try (InputStream in = resolver.openInputStream(uri)) {
                        copy(in, out, remaining);
                    }
                }
            }
            if (file.length() == 0 || file.length() > remaining) throw new IOException("Attachment size limit");
            if (mime == null || !mime.matches("[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+") || mime.length() > 100) {
                mime = "application/octet-stream";
            }
            return new JSONObject().put("id", id).put("name", name).put("mime", mime.toLowerCase(Locale.ROOT))
                    .put("kind", image ? "image" : "file").put("size", file.length());
        } catch (IOException | JSONException | RuntimeException error) {
            file.delete();
            throw error;
        }
    }

    JSONArray encode(JSONArray selected) throws IOException, JSONException {
        JSONArray result = new JSONArray();
        long remaining = MAX_BYTES;
        for (int i = 0; i < selected.length(); i++) {
            JSONObject item = selected.getJSONObject(i);
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try (InputStream in = new java.io.FileInputStream(file(item.getString("id")))) {
                remaining -= copy(in, bytes, remaining);
            }
            result.put(new JSONObject().put("name", item.getString("name")).put("kind", item.getString("kind"))
                    .put("mime", item.getString("mime")).put("data", Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP)));
        }
        return result;
    }

    void remove(JSONObject item) {
        file(item.optString("id")).delete();
    }

    private File file(String id) {
        if (!id.matches("[a-f0-9-]{36}")) throw new IllegalArgumentException("Invalid attachment ID");
        return new File(directory, id);
    }

    private static String filename(ContentResolver resolver, Uri uri) {
        String name = "";
        try (Cursor cursor = resolver.query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) name = cursor.getString(0);
        }
        if (name == null) name = "";
        name = name.replaceAll("[\\p{Cntrl}/\\\\]", "_").trim();
        if (name.isEmpty() || ".".equals(name) || "..".equals(name)) return "附件";
        int length = name.codePointCount(0, name.length());
        return length > 180 ? name.substring(0, name.offsetByCodePoints(0, 180)) : name;
    }

    private static long copy(InputStream in, java.io.OutputStream out, long max) throws IOException {
        if (in == null) throw new IOException("Missing attachment");
        byte[] buffer = new byte[32768];
        long bytes = 0;
        for (int count; (count = in.read(buffer)) != -1;) {
            bytes += count;
            if (bytes > max) throw new IOException("Attachment size limit");
            out.write(buffer, 0, count);
        }
        return bytes;
    }

    private static void saveImage(ContentResolver resolver, Uri uri, FileOutputStream out) throws IOException {
        Bitmap bitmap;
        if (Build.VERSION.SDK_INT >= 28) {
            bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(resolver, uri), (decoder, info, source) -> {
                int width = info.getSize().getWidth();
                int height = info.getSize().getHeight();
                int largest = Math.max(width, height);
                if (largest > IMAGE_SIDE) decoder.setTargetSize(Math.max(1, (int) (width * (double) IMAGE_SIDE / largest)),
                        Math.max(1, (int) (height * (double) IMAGE_SIDE / largest)));
                decoder.setAllocator(ImageDecoder.ALLOCATOR_SOFTWARE);
            });
        } else {
            BitmapFactory.Options options = new BitmapFactory.Options();
            options.inJustDecodeBounds = true;
            try (InputStream in = resolver.openInputStream(uri)) {
                BitmapFactory.decodeStream(in, null, options);
            }
            options.inJustDecodeBounds = false;
            options.inSampleSize = 1;
            while (Math.max(options.outWidth, options.outHeight) / options.inSampleSize > IMAGE_SIDE) options.inSampleSize *= 2;
            try (InputStream in = resolver.openInputStream(uri)) {
                bitmap = BitmapFactory.decodeStream(in, null, options);
            }
        }
        if (bitmap == null) throw new IOException("Invalid image");
        try {
            if (!bitmap.compress(Bitmap.CompressFormat.JPEG, 87, out)) throw new IOException("Cannot encode image");
        } finally {
            bitmap.recycle();
        }
    }
}
