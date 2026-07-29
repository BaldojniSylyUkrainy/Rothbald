# Third-party notices

Rothbald distributes and uses third-party components. Their original licenses
and copyright notices continue to apply.

## Qt and PySide6

The desktop interface uses PySide6 and Qt WebEngine. Qt for Python packages are
available under the LGPLv3/GPLv3 and commercial licensing options described by
the Qt project:

- https://doc.qt.io/qtforpython-6/overviews/qtdoc-lgpl.html
- https://doc.qt.io/qtforpython-6/licenses.html

Release artifacts must preserve the license files supplied by the installed Qt
packages and must not prevent replacement or relinking where the selected
license requires it.

## FFmpeg

Rothbald bundles `ffmpeg` and `ffprobe`. FFmpeg licensing depends on the exact
build configuration:

- https://ffmpeg.org/legal.html

Before public distribution, the release job must retain the license shipped
with the selected FFmpeg build and the owner must verify that build's enabled
components and corresponding source/license obligations.

## whisper.cpp

The Windows Vulkan backend includes whisper.cpp. Its upstream license is copied
into the packaged `licenses` directory by the build process.

## Python packages and model artifacts

The application also bundles the Python packages pinned in the platform lock
files and downloads model artifacts from Hugging Face on first launch. Each
package and model remains subject to its own upstream license. The exact model
repository and immutable revision are recorded in `model-manifest.json`.

This notice is informational and is not legal advice. A project-level LICENSE
must be selected by the repository owner before accepting outside contributions
or granting redistribution rights beyond those of the third-party components.
