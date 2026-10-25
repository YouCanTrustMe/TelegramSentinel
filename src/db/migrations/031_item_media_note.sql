-- What a media marker adds in the digest: a video's length ("0:42") or an album's size ("×5").
ALTER TABLE items ADD COLUMN media_note TEXT;
