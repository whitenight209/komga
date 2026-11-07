package org.gotson.komga.infrastructure.configuration

import org.assertj.core.api.Assertions.assertThat
import org.assertj.core.api.Assertions.assertThatThrownBy
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Path
import kotlin.io.path.writeText

class DecryptionPasswordFileTest {
  @TempDir
  lateinit var directory: Path

  @Test
  fun `file overrides inline password and preserves spaces and special characters`() {
    val file = directory.resolve("password")
    file.writeText("  secret\\\"한글  \r\n")
    val properties = KomgaProperties()
    properties.mediaFileDecryption.password = "inline"
    properties.mediaFileDecryption.passwordFile = file.toString()

    DecryptionPasswordFile.load(properties.mediaFileDecryption)

    assertThat(properties.mediaFileDecryption.password).isEqualTo("  secret\\\"한글  ")
  }

  @Test
  fun `environment file overrides configured file`() {
    val file = directory.resolve("password")
    file.writeText("secret")
    val properties = KomgaProperties()
    properties.mediaFileDecryption.passwordFile = directory.resolve("missing").toString()
    DecryptionPasswordFile.load(properties.mediaFileDecryption, file.toString())
    assertThat(properties.mediaFileDecryption.password).isEqualTo("secret")
  }

  @Test
  fun `without file inline password is retained`() {
    val properties = KomgaProperties()
    properties.mediaFileDecryption.password = "inline"
    DecryptionPasswordFile.load(properties.mediaFileDecryption)
    assertThat(properties.mediaFileDecryption.password).isEqualTo("inline")
  }

  @Test
  fun `missing file fails instead of falling back to inline password`() {
    val properties = KomgaProperties()
    properties.mediaFileDecryption.password = "inline"
    properties.mediaFileDecryption.passwordFile = directory.resolve("missing").toString()
    assertThatThrownBy { DecryptionPasswordFile.load(properties.mediaFileDecryption) }
      .isInstanceOf(IllegalStateException::class.java)
      .hasMessage("Cannot read decryption password file")
  }

  @Test
  fun `empty whitespace and multiline files fail without exposing contents`() {
    val file = directory.resolve("password")
    val properties = KomgaProperties()
    properties.mediaFileDecryption.passwordFile = file.toString()
    listOf("", " \n", "secret\nsecond", "secret\n\n").forEach {
      file.writeText(it)
      assertThatThrownBy { DecryptionPasswordFile.load(properties.mediaFileDecryption) }
        .isInstanceOf(IllegalArgumentException::class.java)
        .hasMessage("Decryption password file must contain a single non-blank line")
    }
  }
}
