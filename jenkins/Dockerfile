# The Jenkins controller: the LTS image plus the plugins pinned in plugins.txt.
# Configuration comes from casc.yaml at start, so nothing is set up by hand.
FROM jenkins/jenkins:2.580.1-lts-jdk21
COPY --chown=jenkins:jenkins plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt --latest false
