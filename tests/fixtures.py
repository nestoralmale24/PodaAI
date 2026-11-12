"""Synthetic legacy application used by the tests (Java/JSP/Struts-like, jars, cp1252, junk)."""
import zipfile
from pathlib import Path


def class_bytes(this_name, refs=()):
    """Minimal valid .class file: constant pool with class entries only."""
    pool = []

    def utf8(text):
        data = text.encode()
        pool.append(b"\x01" + len(data).to_bytes(2, "big") + data)
        return len(pool)

    def cls(name):
        index = utf8(name)
        pool.append(b"\x07" + index.to_bytes(2, "big"))
        return len(pool)

    this_idx = cls(this_name)
    super_idx = cls("java/lang/Object")
    for ref in refs:
        cls(ref)
    out = b"\xca\xfe\xba\xbe" + (0).to_bytes(2, "big") + (52).to_bytes(2, "big")
    out += (len(pool) + 1).to_bytes(2, "big") + b"".join(pool)
    out += (0x21).to_bytes(2, "big") + this_idx.to_bytes(2, "big") + super_idx.to_bytes(2, "big") + b"\x00\x00" * 4
    return out


def make_jar(path, classes, extra=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, refs in classes.items():
            zf.writestr(name + ".class", class_bytes(name, refs))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)


MAIN_SERVLET = """package com.acme.web;

import java.util.List;
import javax.servlet.http.HttpServlet;
import org.apache.struts.action.Action;
import com.acme.util.Helper;
import com.acme.util.Legacy1252;

public class MainServlet extends HttpServlet {

    private Action action = null;

    public void doGet() {
        Helper.help(action);
        Legacy1252.run();
        // String old = Helper.legacy();
        // Helper.print(old);
        if (false) {
            Helper.debug();
        }
    }

    private void neverUsed() {
        Helper.help(null);
    }
}
"""

LEGACY_1252 = ("package com.acme.util;\r\n\r\n/** Clase con eñes y tildes: año, versión. */\r\n"
               "public class Legacy1252 {\r\n    public static void run() {\r\n"
               "        String s = \"año\";\r\n        // System.out.println(\"versión antigua\");\r\n"
               "        // s = s.trim();\r\n    }\r\n}\r\n")


def build_fixture(root: Path) -> None:
    def write(rel, text, encoding="utf-8"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))

    write("pom.xml", """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>legacy</artifactId>
  <version>1.0</version>
  <dependencies>
    <dependency>
      <groupId>org.apache.struts</groupId>
      <artifactId>struts-fake</artifactId>
      <version>1.0</version>
    </dependency>
    <dependency>
      <groupId>commons-lang</groupId>
      <artifactId>commons-lang</artifactId>
      <version>2.6</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.12</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
""")
    write("WebContent/WEB-INF/web.xml", """<?xml version="1.0"?>
<web-app>
  <servlet>
    <servlet-name>main</servlet-name>
    <servlet-class>com.acme.web.MainServlet</servlet-class>
  </servlet>
  <welcome-file-list><welcome-file>index.jsp</welcome-file></welcome-file-list>
  <!--
  <servlet>
    <servlet-name>old</servlet-name>
    <servlet-class>com.acme.web.OldServlet</servlet-class>
  </servlet>
  -->
</web-app>
""")
    write("WebContent/index.jsp", """<%@ page contentType="text/html" %>
<html><head><script src="js/app.js"></script></head>
<body><img src="images/logo.png"/>
<!-- <div class="banner">Promo</div> -->
</body></html>
""")
    write("WebContent/old/pantallaVieja.jsp", "<html><body>pantalla antigua</body></html>\n")
    write("WebContent/protected/kept.jsp", "<html><body>enlazada desde fuera</body></html>\n")
    write("WebContent/js/app.js", "function used() { return 1; }\nfunction neverCalled() { return 2; }\nused();\n")
    write("WebContent/js/vendor/skinx/a.css", ".x { color: red; }\n")
    write("WebContent/js/vendor/skinx/readme.txt", "Skin de ejemplo\n")
    (root / "WebContent/js/vendor/skinx/b.gif").write_bytes(b"GIF89a\x01\x00\x01\x00\x00\x00\x00")
    (root / "WebContent/images").mkdir(parents=True, exist_ok=True)
    (root / "WebContent/images/logo.png").write_bytes(b"\x89PNG\r\n\x1a\nlogo")
    (root / "WebContent/images/unused.png").write_bytes(b"\x89PNG\r\n\x1a\nunused")
    write("src/com/acme/web/MainServlet.java", MAIN_SERVLET)
    write("src/com/acme/util/Helper.java", """package com.acme.util;

public class Helper {
    public static void help(Object o) { }
    public static void debug() { }
    public static String legacy() { return ""; }
    public static void print(String s) { }
    public static void orphanPublic() { }
}
""")
    write("src/com/acme/util/OldHelper.java", "package com.acme.util;\n\npublic class OldHelper {\n    public void x() { }\n}\n")
    write("src/com/acme/util/KeepMeHelper.java", "package com.acme.util;\n\npublic class KeepMeHelper {\n}\n")
    write("src/com/acme/util/Legacy1252.java", LEGACY_1252, encoding="cp1252")
    write("docs/notes.md", "OldHelper was replaced by Helper.\n")
    write("scripts/run.sh", "#!/bin/sh\njava -cp . com.acme.web.MainServlet\n")
    (root / "core.12345").write_bytes(b"\x7fELF" + b"\x00" * 60)
    (root / "Thumbs.db").write_bytes(b"\x00\x01thumbs")
    classes = root / "WebContent/WEB-INF/classes"
    (classes / "com/acme/util").mkdir(parents=True, exist_ok=True)
    (classes / "com/acme/util/Helper.class").write_bytes(class_bytes("com/acme/util/Helper"))
    (classes / "com/thirdparty").mkdir(parents=True, exist_ok=True)
    (classes / "com/thirdparty/Only.class").write_bytes(class_bytes("com/thirdparty/Only"))
    lib = root / "WebContent/WEB-INF/lib"
    make_jar(lib / "struts-fake.jar", {"org/apache/struts/action/Action": ["org/apache/commons/beanutils/BeanUtils"]})
    make_jar(lib / "beanutils-fake.jar", {"org/apache/commons/beanutils/BeanUtils": []})
    make_jar(lib / "unused-lib-1.0.jar", {"com/unused/Thing": []})
    make_jar(lib / "ojdbc-keepme.jar", {"oracle/jdbc/OracleDriver": []},
             {"META-INF/services/java.sql.Driver": "oracle.jdbc.OracleDriver\n"})
