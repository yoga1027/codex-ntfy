package local.codexntfy.opush;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

public final class CameraOutput extends ContentProvider {
    static Uri uri(Context context, String file) {
        return new Uri.Builder().scheme("content").authority(context.getPackageName() + ".camera")
                .appendPath(file).build();
    }

    static File file(Context context, String name) {
        if (name == null || !name.matches("[a-f0-9-]{36}\\.jpg")) throw new IllegalArgumentException("Invalid photo");
        return new File(new File(context.getCacheDir(), "camera"), name);
    }

    @Override public boolean onCreate() { return true; }

    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File photo = file(getContext(), uri.getLastPathSegment());
        if (!photo.isFile()) throw new FileNotFoundException(uri.toString());
        return ParcelFileDescriptor.open(photo, ParcelFileDescriptor.parseMode(mode));
    }

    @Override public String getType(Uri uri) { return "image/jpeg"; }

    @Override public Cursor query(Uri uri, String[] projection, String where, String[] args, String order) {
        File photo = file(getContext(), uri.getLastPathSegment());
        String[] columns = projection == null ? new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE} : projection;
        MatrixCursor result = new MatrixCursor(columns);
        MatrixCursor.RowBuilder row = result.newRow();
        for (String column : columns) {
            if (OpenableColumns.DISPLAY_NAME.equals(column)) row.add(photo.getName());
            else if (OpenableColumns.SIZE.equals(column)) row.add(photo.length());
            else row.add(null);
        }
        return result;
    }

    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int update(Uri uri, ContentValues values, String where, String[] args) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String where, String[] args) { throw new UnsupportedOperationException(); }
}
