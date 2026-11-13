# syntax=docker/dockerfile:1
FROM maven:3.9.6-eclipse-temurin-21 AS theme-package
WORKDIR /build
COPY --from=theme pom.xml LICENSE ./
COPY --from=theme src src
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp verify

FROM quay.io/keycloak/keycloak:26.8.2
ADD --checksum=sha256:17323681b6303931922da4fc61097de50e494ef0c53fb03ba2036960366f7687 --chown=keycloak:keycloak \
    https://github.com/Sats-Lottu/keycloak-lnurl-auth/releases/download/v0.2.1/keycloak-lnurl-auth-0.2.1.jar \
    /opt/keycloak/providers/keycloak-lnurl-auth-0.2.1.jar
ADD --checksum=sha256:ce63d9486d8672f1ebd88b1da7932d0612ddfeff221fcd2d97f4e66f4064a91a --chown=keycloak:keycloak \
    https://github.com/Sats-Lottu/keycloak-nostr-auth/releases/download/v0.2.1/keycloak-nostr-auth-0.2.1.jar \
    /opt/keycloak/providers/keycloak-nostr-auth-0.2.1.jar
COPY --from=theme-package --chown=keycloak:keycloak \
    /build/target/keycloak-sats-lottu-theme-*.jar /opt/keycloak/providers/
