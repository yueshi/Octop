import { useEffect, useState } from "react";

import { requestBlob } from "../api/request";

/**
 * Load an image from the API as an object URL.
 *
 * The API needs the JWT, which an ``<img src>`` cannot send, so the image is
 * fetched with the auth header and handed to the tag as a blob URL. Returns
 * ``null`` while loading and when the image cannot be loaded, so callers can
 * fall back to a placeholder.
 */
export function usePreviewImage(path: string | null): string | null {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!path) {
      setUrl(null);
      return;
    }
    let alive = true;
    let objectUrl: string | null = null;
    void requestBlob(path)
      .then((blob) => {
        if (!alive) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (alive) setUrl(null);
      });
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  return url;
}
