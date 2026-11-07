package org.gotson.komga.infrastructure.configuration

import kotlin.io.path.Path
import kotlin.io.path.readText

/** File loading is kept separate from upstream configuration properties to limit merge conflicts. */
internal object DecryptionPasswordFile {
  fun load(
    properties: KomgaProperties.MediaFileDecryption,
    environmentFile: String? = null,
  ) {
    val file = environmentFile ?: properties.passwordFile ?: return
    require(file.isNotBlank()) { "Decryption password file path must not be blank" }
    val secret =
      try {
        Path(file).readText(Charsets.UTF_8).removeSuffix("\n").removeSuffix("\r")
      } catch (_: Exception) {
        throw IllegalStateException("Cannot read decryption password file")
      }
    require(secret.isNotBlank() && '\n' !in secret && '\r' !in secret) {
      "Decryption password file must contain a single non-blank line"
    }
    properties.password = secret
  }
}
