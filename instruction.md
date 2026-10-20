# Vaultline

## 1. Overview

Vaultline is a versioned object storage service. Clients store objects
(arbitrary bytes) under keys in buckets, keep every version of an object,
delete objects with delete markers, and list objects and versions with prefix
grouping and pagination. All state lives on the local filesystem and survives
termination of the service process at any instant. The API is HTTP/1.1 with
JSON bodies for everything except object data.

## 2. Run contract

- **Location.** The service is the Node.js project at the root of your
  workspace. The project root contains `config.toml`. Its `[setup]` commands
  are run once before the first start, and its `[run]` command starts the
  service, with the project root as the working directory. You may change
  both commands; keep `ports = [8080]` under `[run]`.
  The `[setup]` commands finish within 60 seconds.
- **Runtime.** Node.js v20.20.2 with npm 10.8.2 is installed. JavaScript only
  (ES modules or CommonJS).
- **Dependencies.** Only Node.js built-in modules. No third-party code in any
  form: no npm packages, no vendored or copied libraries, no WebAssembly
  modules, no native addons. The setup commands and the service run without
  network access.
- **Environment.** `VAULTLINE_PORT` is the TCP port; it is always the port
  listed in `[run]` `ports`. `VAULTLINE_DATA_DIR` is
  the absolute path of the data directory. The directory exists and is empty
  before the first start, and the same directory is passed on every restart.
  All persistent state must be stored under it.
- **Listening.** Accept HTTP connections on `127.0.0.1` at `VAULTLINE_PORT`.
  Clients connect to `127.0.0.1`.
- **Process.** The service is started in a new process group. It must run in
  the foreground for its whole lifetime and must not daemonize, detach, or
  start any process that leaves that process group. Child processes and
  worker threads inside the group are allowed. The service is stopped only by
  SIGKILL sent to the whole process group, at any instant, and is then
  started again with the same environment.
- **Readiness.** `GET /health` returns 200 only once the service serves
  requests against fully recovered state. Before that, connections are refused
  or `/health` returns 503. The service must be ready within 15 seconds of
  process start at the scale in §10. The `/health` body is unspecified.
- Output on stdout and stderr is ignored.

## 3. Concepts

### 3.1 Buckets

A bucket name is 3–63 characters from `a`–`z`, `0`–`9` and `-`, and starts and
ends with a letter or digit. A new bucket is empty and in versioning state
`Unversioned`.

### 3.2 Keys

A key is a byte sequence that is valid UTF-8, 1–1024 bytes long, and contains
no code point in U+0000–U+001F and not U+007F. Every other code point is
allowed. Keys are compared byte for byte: there is no Unicode normalization,
and keys are case-sensitive. Keys are opaque bytes: `/` and `.` have no
special meaning, and `a` and `a/b` can exist at the same time.

**Key order.** Everywhere in this API, keys and other strings are ordered
lexicographically by the unsigned bytes of their UTF-8 encoding. A string that
is a prefix of another sorts first.

### 3.3 Versions

Each key has a **history**: a sequence of versions, ordered by commit. A
version is either an **object version** (a body plus metadata) or a **delete
marker** (no body). The newest version is the key's **current version**. A key
**has a current object** if its newest version is an object version.

Every version has a `versionId` and a `lastModified` time. An object version
also has an `etag`, a `size` (body length in bytes) and a content type.

**Order.** Every version occupies a fixed place in its key's history,
determined by when it was committed. A version committed later is always
newer, including across restarts of the service. If a write was acknowledged
(§8) before the client began sending another write to the same key, the
second write's version is newer. Removing a version never changes the order of the remaining ones.

**Version IDs.** A version ID is either the four-character string `null` or
an opaque string of 1–64 characters from `A`–`Z`, `a`–`z`, `0`–`9`, `.`,
`_`, `~` and `-` that is not `null`. Version IDs carry no ordering. Within
one lifetime of a bucket (from its creation to its deletion), for each key:
once the service has sent any byte of a response that contains a version ID
other than `null` for that key, in any body field or header, that ID never
identifies a different version of that key afterwards. This holds whether the
original version still exists, was removed, or the service restarted in
between. The ID `null` names whichever version currently holds it (§3.4). A
**new** non-`null` ID for a key is one that no existing version of that key
has and that no response sent in this bucket lifetime has contained for that
key.

**ETag.** The ETag of an object version is the lowercase hexadecimal MD5
digest of its body, enclosed in double quotes. The same string, quotes
included, appears in headers and in JSON, for example
`"etag": "\"d41d8cd98f00b204e9800998ecf8427e\""` (the ETag of an empty body).

**lastModified.** An ISO-8601 UTC timestamp with milliseconds,
`YYYY-MM-DDTHH:mm:ss.sssZ`, taken from the service's clock when the version
is committed and adjusted so that within one key's history it never decreases
from older to newer versions. No accuracy relative to real time is promised.
It carries no other meaning and is never used for ordering.

**Content type.** Stored per object version exactly as received in the
`Content-Type` request header of the PUT, and returned unchanged;
`application/octet-stream` if the header is absent.

### 3.4 Versioning states

A bucket is in one of three states: `Unversioned` (initial), `Enabled` or
`Suspended`. It can be set to `Enabled` or `Suspended` from any state. It can
never be set back to `Unversioned`. Changing the state never modifies existing
versions.

Effect of writes on a key's history:

| Operation | `Enabled` | `Suspended` and `Unversioned` |
|-----------|-----------|-------------------------------|
| PUT object | Adds a new object version with a new non-`null` ID (§3.3) as the newest version. | Removes the key's `null` version, if any (object version or marker). Adds a new object version with ID `null` as the **newest** version. Other versions are kept. |
| DELETE object, no `versionId` | Adds a new delete marker with a new non-`null` ID as the newest version. This happens whether or not the key has versions, and even if the current version is already a marker. | `Suspended`: removes the key's `null` version, if any, and adds a delete marker with ID `null` as the newest version, even if the key had no versions. `Unversioned`: removes the key's `null` version, if any; no marker is added. |
| DELETE object with `versionId` | Permanently removes exactly that version (object version or marker). If it was the newest, the next-newest version, which may be a marker, becomes current. | Same. |

A key therefore has at most one `null` version at any time. In an
`Unversioned` bucket every version is `null`, so each key has at most one
version and never a marker. Example: in a `Suspended` bucket, a history
`[v7, null, v2]` (newest first) becomes `[null, v7, v2]` after a PUT. The new
`null` version is newest; the old one is gone. After a bucket goes from
`Unversioned` to `Enabled`, an existing `null` version stays in place, and the
next PUT adds a non-`null` version above it.

## 4. Request conventions

- **Bucket names** appear literally in the path.
- **Query parameters** are decoded in a single left-to-right pass that
  replaces `+` with a space and each `%XX` with the byte it encodes, so `%2B`
  decodes to `+`. Parameter names are matched exactly and case-sensitively
  after decoding. A decoded value must be valid UTF-8; invalid sequences are
  errors (§9) and must not be replaced, except that a `versionId` that is not
  valid UTF-8 is treated as an ID that no version has. Values of parameters an
  endpoint does not use, including unknown names and `startAfter` on the
  version listing, are ignored without being decoded.
- **Request bodies.** A body of length 0 is valid. If the client closes the
  connection before sending all declared body bytes, the request has no
  effect.
- **Request headers** that this document does not mention are ignored, apart
  from HTTP/1.1 connection handling.
- **Responses.** JSON responses carry a `Content-Type` whose media type is
  `application/json`; parameters such as `charset` are allowed. Responses may
  include additional JSON fields and additional headers. HEAD responses never
  have a body.
- **Connections.** Clients may reuse connections. The service may close idle
  connections.
- **In flight.** A request is in flight from the first byte the client sends
  until the client has received the full response or the connection has
  closed.

## 5. Endpoints

In paths, `{bucket}` is a bucket name. `key` is required on object endpoints.

| Method and path | Success |
|-----------------|---------|
| `GET /health` | 200 (§2) |
| `PUT /buckets/{bucket}` | 201 `{"name":"<bucket>"}` |
| `DELETE /buckets/{bucket}` | 200 `{"name":"<bucket>"}` |
| `GET /buckets` | 200 |
| `GET /buckets/{bucket}/versioning` | 200 |
| `PUT /buckets/{bucket}/versioning` | 200 |
| `PUT /buckets/{bucket}/object?key=K` | 200 |
| `GET` / `HEAD /buckets/{bucket}/object?key=K[&versionId=V]` | 200 |
| `DELETE /buckets/{bucket}/object?key=K[&versionId=V]` | 200 |
| `GET /buckets/{bucket}/objects` | 200 (§6) |
| `GET /buckets/{bucket}/versions` | 200 (§6) |

### 5.1 Buckets

- **Create** `PUT /buckets/{bucket}`: creates an empty `Unversioned` bucket.
  Errors: `InvalidBucketName`, `BucketAlreadyExists`.
- **Delete** `DELETE /buckets/{bucket}`: allowed only if no key in the bucket
  has any version, including delete markers. Errors: `NoSuchBucket`,
  `BucketNotEmpty`. A deleted name can be created again. The new bucket is
  empty and `Unversioned`; no version, marker or versioning state of the
  deleted bucket ever reappears in it. Version ID strings may repeat.
- **List** `GET /buckets`: `{"buckets":[{"name":"<bucket>"}, …]}`, all
  buckets in key order (§3.2).

### 5.2 Versioning state

- `GET /buckets/{bucket}/versioning` returns
  `{"status":"Unversioned"|"Enabled"|"Suspended"}`.
- `PUT /buckets/{bucket}/versioning` takes a JSON object body with a field
  `status` of `"Enabled"` or `"Suspended"`, case-sensitive. Other fields are
  ignored, and the
  request `Content-Type` is ignored. It returns `{"status":"<state after
  the request>"}`. Setting the current state succeeds and changes nothing. A
  body that is not a syntactically valid JSON object (including an empty
  body), has no `status`, or has any other `status` value (including
  `"Unversioned"`) → `InvalidArgument`.

### 5.3 PUT object

`PUT /buckets/{bucket}/object?key=K` with the object body applies the PUT
row of §3.4 and returns
`{"key":"<K>","versionId":"<id>","etag":"<etag>","size":<n>,"lastModified":"<time>"}`
for the new version. Bodies of up to 16,777,216 bytes are accepted and
stored. If `Content-Length` is larger, the response is 413 `EntityTooLarge`.
Any error response to PUT object may be sent before the body is read; the
request then has no effect, and the service may close the connection after
the response, provided a client that sends the entire declared body receives
it.

### 5.4 GET and HEAD object

Without `versionId`, the target is the key's current version. With
`versionId=V` (`V` may be `null`), the target is the key's version with ID `V`.

| Situation | Response |
|-----------|----------|
| Target is an object version | 200, body = the object body |
| No `versionId`; key has no versions | 404 `NoSuchKey` |
| No `versionId`; current version is a marker | 404 `NoSuchKey`, with marker headers |
| `versionId` given; no version of the key has it | 404 `NoSuchVersion` |
| `versionId` names a marker | 405 `MethodNotAllowed`, with marker headers |

A 200 response carries `ETag`, `Content-Length` (the size), `Content-Type`
(the stored content type), `X-Vaultline-Version-Id` and
`X-Vaultline-Last-Modified`. **Marker headers** are
`X-Vaultline-Delete-Marker: true` plus the marker's `X-Vaultline-Version-Id`
and `X-Vaultline-Last-Modified`. `X-Vaultline-Delete-Marker` appears only on
marker responses. HEAD returns the same status and headers as GET, without a
body; on a 200, `Content-Length` is still the object size. On a HEAD error
response `Content-Length` is unspecified. A 404 or 405 GET response carries
the error body of §9. Marker responses carry no `ETag`.

### 5.5 DELETE object

Applies the matching DELETE row of §3.4. Every successful response is 200
`{"versionId":"<string>","deleteMarker":<bool>}`. `versionId` is always a
JSON string; the `null` ID is the string `"null"`.

| Request | State | Body |
|---------|-------|------|
| no `versionId` | `Enabled` | `{"versionId":"<new marker ID>","deleteMarker":true}` |
| no `versionId` | `Suspended` | `{"versionId":"null","deleteMarker":true}` |
| no `versionId` | `Unversioned` | `{"versionId":"null","deleteMarker":false}`, whether or not the key had a version |
| `versionId=V`, V exists | any | `{"versionId":"V","deleteMarker":<true if V was a marker>}` |
| `versionId=V`, V does not exist | any | 404 `NoSuchVersion` |

A DELETE without `versionId` and with a valid `key` always succeeds when the
bucket exists. A DELETE
with a `versionId` that names no version fails, including a retry of a DELETE
that already succeeded.

## 6. Listing

`GET /buckets/{bucket}/objects` lists current objects.
`GET /buckets/{bucket}/versions` lists all versions and delete markers.

### 6.1 Parameters

- `prefix`: only keys that start with these bytes are considered. Default
  empty. `prefix`, `delimiter` and `startAfter` may be any valid UTF-8 value,
  including empty.
- `delimiter`: groups keys (§6.2). Absent or empty means no grouping.
- `maxKeys`: page size, an integer 1–1000 written as decimal digits with no
  sign and no leading zeros. Default 1000. Anything else → `InvalidArgument`.
- `token`: continues a previous listing (§6.4).
- `startAfter` (objects listing only): only keys strictly greater than this
  string (§3.2) are considered. `token` and `startAfter` together →
  `InvalidArgument`.

### 6.2 The listing sequence

**Items.** The objects listing has one entry per considered key that has a
current object. The version listing has one entry per version (object version
or marker) of each considered key.

**Grouping.** If `delimiter` is non-empty, let R be an entry's key with
`prefix` removed. If R contains the delimiter, the entry is replaced by the
**common prefix** `prefix` + R up to and including the first occurrence of
the delimiter in R. Occurrences inside `prefix` are not considered. Each
distinct common prefix is one item. In the objects listing a common prefix
therefore appears only if at least one key under it has a current object.
With `startAfter`, keys are filtered first and grouped afterwards, so a common
prefix that is not greater than `startAfter` can appear if a key under it is.

**Order.** Items are ordered by key (§3.2); a common prefix sorts by its own
string. In the version listing, the entries of one key are ordered newest
first.

### 6.3 Pages

A page holds the first `maxKeys` items of the sequence that follow the start
place: the beginning of the sequence, or the place denoted by `token`. Entries
and common prefixes each count as one item. The response is:

```json
{"entries":[…],"commonPrefixes":["…"],"isTruncated":false,"nextToken":null}
```

`entries` and `commonPrefixes` each list their items in sequence order.
`isTruncated` is true if at least one item follows the page's last item when
the page is served. `nextToken` is a string if `isTruncated` is true and
`null` otherwise. If no item follows the start place, `entries` and
`commonPrefixes` are empty, `isTruncated` is false and `nextToken` is `null`.
While writes are in flight, all fields of one entry come from one version of
its key at one instant during the request, and `isTruncated` reflects some
state of the bucket during the request.

An objects-listing entry describes the key's current version:

```json
{"key":"k","versionId":"Q1","etag":"\"0cc175b9c0f1b6a831c399e269772661\"","size":1,"lastModified":"2026-01-01T00:00:00.000Z"}
```

A version-listing entry for an object version, then for a marker:

```json
{"key":"k","versionId":"Q1","isLatest":false,"deleteMarker":false,"etag":"\"0cc175b9c0f1b6a831c399e269772661\"","size":1,"lastModified":"2026-01-01T00:00:00.000Z"}
{"key":"k","versionId":"Q2","isLatest":true,"deleteMarker":true,"etag":null,"size":null,"lastModified":"2026-01-01T00:00:01.000Z"}
```

`isLatest` is true exactly for the newest version of the key. `deleteMarker`
is false for object versions; for markers it is true and `etag` and `size`
are `null`.

### 6.4 Tokens

A token is opaque. It is bound to the endpoint, bucket, `prefix` and
`delimiter` of the request that returned it, compared by effective value (an
absent `prefix` or `delimiter` equals an empty one), so a continuation
request repeats the same `prefix` and `delimiter`; `maxKeys` may differ. A
token can be used any number of times, never expires, and remains valid
across restarts of the service. An empty `token`, a token the service cannot
interpret, or a token issued for another endpoint, bucket, `prefix` or
`delimiter` → `InvalidToken`. The service is not required to detect a token
that was altered or made up.

A token denotes the place in the listing sequence just after the last item of
its page. Within one key's entries in the version listing, the place is the
version's place in the key's history (§3.3), not its version ID. The next
page lists the items that follow that place **in the state of the bucket when
the next page is requested**, even if the item the previous page ended on no
longer exists. After a common prefix, the place is after every key that
starts with that common prefix.

An **iteration** is a chain of pages: it starts with a request without
`token`, and each further page is requested with the `nextToken` of the page
before it. Each use of a token starts its own continuation, and the
guarantees below apply to each chain separately. An objects-listing entry is identified by its key and
current version ID; a version-listing entry by its key and its place in the
key's history, not by its version ID; a common prefix by its string. Other
fields, including `isLatest`, are not part of identity. Listings are not
snapshots, but while writes happen between or during page requests, one
iteration guarantees:

1. No item appears twice. In the objects listing no key appears twice; the
   version listing lists a key once per version, and no (key, version ID)
   pair appears twice. No common prefix appears twice.
2. Every item of the listing sequence for the iteration's `prefix`,
   `delimiter` and `startAfter` whose identity is present during the whole
   iteration appears exactly once.
3. Items created or removed during the iteration may or may not appear.
4. Items appear in strictly increasing sequence order across pages.

## 7. Consistency and concurrency

- Operations on one key are linearizable. Each operation that changes or
  reads state takes effect at a single instant between the moment the full
  request has been received and the moment its response is sent. Early error
  responses (§5.3) are allowed as stated there.
- Bucket create, bucket delete, `GET /buckets`, and versioning GET and PUT are
  linearizable. Of concurrent creates of one name, exactly one returns 201 unless a delete
  of that name succeeds in between.
- A read returns one complete version. The headers and body of one response
  always belong to the same version.
- Concurrent PUTs to one key in an `Enabled` bucket each create a distinct
  version. In `Suspended` and `Unversioned` buckets, concurrent PUTs alone
  leave exactly one `null` version, holding one complete body.
- There is no atomicity across keys.

## 8. Durability and crash recovery

A write is **acknowledged** when the client has received the complete
response (status, headers and body) with a 2xx status. Writes are object PUT,
both kinds of object DELETE, versioning-state changes, and bucket create and
delete. The service may be terminated at any instant, any number of times,
including during startup and recovery. Durability means surviving such
termination of the service process (§2); host crashes, power loss and storage
faults are outside this specification. A write can be applied only after its
full request has been received. A write that was not acknowledged and that no
response has reflected may be lost at termination; it is then not applied and
never counts as committed. After every restart:

1. **Acknowledged writes are durable.** Each acknowledged write's effect is
   present until a later operation changes it.
2. **Effects are durable before they are revealed.** Before the service sends
   any byte of a response that reflects the effect of a write, that effect is
   durable: it survives every later termination exactly as if the write had
   been acknowledged, until a later operation changes it. This covers the
   write's own response and every other response, including responses that
   reflect a removal.
3. **In-flight writes are all-or-nothing.** A write that was in flight is
   either fully applied or not applied at all. Once a response after a restart
   has shown either outcome, every later response agrees, across any number of
   further restarts, until a later operation changes it.
4. **The recovered state is consistent.** The following hold at any time,
   including after every restart. A property of a key may fail only while a
   write to that key is in flight; the `GET /buckets` property only while a
   create or delete of that bucket name is in flight. A version is **listed**
   when it appears on some page of an iteration (§6.4) of the version listing
   without `prefix` or `delimiter`.
   - Every object version is either fully readable, with a body that matches
     its size and ETag, or absent.
   - A version answers GET and HEAD by its ID (200 for an object version, 405
     for a marker) exactly when it is listed, with the ID and time of its
     listing entry, and for an object version also its ETag and size.
   - GET and HEAD without `versionId` answer for the key's newest listed
     version.
   - Every marker creation, `null` replacement, version removal and
     versioning-state change is either fully applied or not applied.
   - `GET /buckets` lists a bucket exactly when its other endpoints do not
     answer `NoSuchBucket`.
   - A bucket re-created with a deleted bucket's name contains no version,
     marker or versioning state of the deleted bucket.
5. A version committed after a restart is newer than every version of that
   key committed before it. The version ID rule (§3.3) and tokens (§6.4) keep
   working across restarts.

## 9. Errors

Error responses have the body
`{"error":{"code":"<code>","message":"<text>"}}`. The message is free text.

| Code | Status | Returned when |
|------|--------|---------------|
| `InvalidBucketName` | 400 | `{bucket}` violates §3.1 (any endpoint with `{bucket}`) |
| `InvalidKey` | 400 | `key` is missing, empty, longer than 1024 bytes, not valid UTF-8, or contains a forbidden code point |
| `InvalidArgument` | 400 | invalid `maxKeys` (including not valid UTF-8); `prefix`, `delimiter` or `startAfter` not valid UTF-8; `token` with `startAfter`; invalid versioning body |
| `InvalidToken` | 400 | `token` is empty, not valid UTF-8, cannot be interpreted, or was issued for another endpoint, bucket, `prefix` or `delimiter` |
| `EntityTooLarge` | 413 | `Content-Length` above 16,777,216 on PUT object |
| `NoSuchBucket` | 404 | the bucket does not exist |
| `NoSuchKey` | 404 | GET/HEAD without `versionId` and the key has no current object |
| `NoSuchVersion` | 404 | a `versionId` is given (including empty or not valid UTF-8) and no version of the key has it |
| `MethodNotAllowed` | 405 | GET/HEAD with a `versionId` that names a delete marker |
| `BucketAlreadyExists` | 409 | create of an existing bucket |
| `BucketNotEmpty` | 409 | delete of a bucket that has any version or marker |
| `InternalError` | 500 | an unexpected failure |

For requests within §10 and §11, the service returns only the statuses in
§2, §5 and this table. A write that returns an error other than 500 has no
effect. A correct service
does not return 500 within the limits of §10. After a 500, the outcome of the
write is unspecified.

**Precedence.** When several errors apply, the first group in this list
wins: (1) 400 and 413 errors (among these, any applicable one may be
returned); (2) `NoSuchBucket`; (3) `NoSuchKey` or `NoSuchVersion`;
(4) `MethodNotAllowed`; (5) 409 errors.

## 10. Limits

The maxima count live quantities: existing buckets, keys, versions and
stored bodies. The last three rows instead count, across all restarts of one
data directory and including requests that fail, only what they name:
requests with a declared body above 1 MiB, write requests, and the declared
body bytes of write requests. Every request not interrupted by termination
must complete within 10 seconds of being fully sent, at this scale and with
16 connections in use. A request is complete when the client has received
the full response.

| Quantity | Maximum |
|----------|---------|
| Buckets | 10 |
| Distinct keys per bucket | 2,500 |
| Versions and markers per key | 100 |
| Versions and markers per bucket | 5,000 |
| Total body bytes stored | 128 MiB |
| Concurrent connections | 16 |
| Declared `Content-Length` of one request | 33,554,432 bytes |
| Request target, excluding the `token` value | 8,192 bytes |
| Request line and header fields, excluding the `token` value | 10,240 bytes, at most 50 fields |
| Requests with a declared body above 1 MiB, per data directory | 8 |
| Write requests per data directory | 60,000 |
| Declared body bytes of write requests, per data directory | 256 MiB |

## 11. What clients do and never do

Requests that break these promises are outside this specification.

- Clients stay within the limits of §10.
- Clients use only the methods and paths listed in §5.
- Before `GET /health` has returned 200, clients send no request other than
  `GET /health`.
- Clients use HTTP/1.1 and never pipeline: at most one request is in flight
  per connection. They never send `Expect`.
- Clients send every bucket name literally as one path segment, never
  percent-encoded, and never send empty or extra path segments.
- Clients never send malformed `%` escapes, a repeated parameter, or a
  parameter without `=`.
- Clients send a body only on PUT object and versioning PUT, always with
  `Content-Length` and never with chunked transfer encoding.
- Clients send at most one `Content-Type` header, with a non-empty ASCII
  media-type value.
- Versioning PUT bodies are UTF-8 without a byte-order mark and contain at
  most one `status` field.
- No request whose path names a bucket is in flight at the same time as a
  create or delete of that bucket name, in either order, except other
  creates and deletes of that name.
- No other request whose path names a bucket is in flight at the same time
  as a versioning PUT for that bucket, in either order.
- As `token`, clients send only a value the service returned as `nextToken`,
  or the empty string.
- Clients never send a token after the bucket it was issued for has been
  deleted, whether or not the name was created again.
- In query parameter names and values, including tokens, clients
  percent-encode every byte outside the RFC 3986 unreserved set (`A`–`Z`,
  `a`–`z`, `0`–`9`, `-`, `.`, `_`, `~`) and no other byte, and never send a
  literal `+`.
