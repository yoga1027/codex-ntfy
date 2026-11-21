package local.codexntfy.opush;

import android.content.ContentResolver;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageDecoder;
import android.net.Uri;
import android.os.Build;
import android.util.AtomicFile;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.EnumMap;

/** Stores selected artwork locally without retaining access to the photo library. */
final class PhotoAppearance {
    enum Image {
        AVATAR(R.string.appearance_avatar, 384),
        HOME(R.string.appearance_home, 1600),
        CHAT(R.string.appearance_chat, 1600);

        final int label;
        final int maxSide;

        Image(int label, int maxSide) {
            this.label = label;
            this.maxSide = maxSide;
        }
    }

    private final Context context;
    private final EnumMap<Image, Bitmap> images = new EnumMap<>(Image.class);

    PhotoAppearance(Context context) {
        this.context = context.getApplicationContext();
    }

    synchronized Bitmap get(Image image) {
        if (!images.containsKey(image)) {
            File bitmap = file(image).getBaseFile();
            images.put(image, bitmap.isFile() ? BitmapFactory.decodeFile(bitmap.getPath()) : null);
        }
        return images.get(image);
    }

    void importPhoto(Image image, Uri uri) throws IOException {
        Bitmap bitmap = decode(uri, image.maxSide);
        if (bitmap == null) {
            throw new IOException("The selected image could not be decoded");
        }
        synchronized (this) {
            AtomicFile file = file(image);
            FileOutputStream output = file.startWrite();
            try {
                Bitmap.CompressFormat format = bitmap.hasAlpha()
                        ? Bitmap.CompressFormat.PNG : Bitmap.CompressFormat.JPEG;
                if (!bitmap.compress(format, 86, output)) {
                    throw new IOException("Could not save the selected image");
                }
                file.finishWrite(output);
                images.put(image, bitmap);
            } catch (IOException | RuntimeException error) {
                file.failWrite(output);
                throw error;
            }
        }
    }

    synchronized void reset(Image image) {
        file(image).delete();
        images.put(image, null);
    }

    private AtomicFile file(Image image) {
        return new AtomicFile(new File(context.getFilesDir(), "appearance-" + image.name() + ".img"));
    }

    private Bitmap decode(Uri uri, int limit) throws IOException {
        ContentResolver resolver = context.getContentResolver();
        if (Build.VERSION.SDK_INT >= 28) {
            return ImageDecoder.decodeBitmap(ImageDecoder.createSource(resolver, uri), (decoder, info, source) -> {
                int width = info.getSize().getWidth();
                int height = info.getSize().getHeight();
                float scale = Math.min(1f, (float) limit / Math.max(width, height));
                decoder.setTargetSize(Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale)));
                decoder.setAllocator(ImageDecoder.ALLOCATOR_SOFTWARE);
            });
        }
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inJustDecodeBounds = true;
        try (InputStream stream = resolver.openInputStream(uri)) {
            BitmapFactory.decodeStream(stream, null, options);
        }
        options.inSampleSize = 1;
        while (Math.max(options.outWidth, options.outHeight) / options.inSampleSize > limit) {
            options.inSampleSize *= 2;
        }
        options.inJustDecodeBounds = false;
        try (InputStream stream = resolver.openInputStream(uri)) {
            return BitmapFactory.decodeStream(stream, null, options);
        }
    }
}
