"use strict";
// Amélioration progressive : sans JavaScript, tout reste affiché et utilisable.
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var ETATS = { inactif: "Aucune analyse en cours", en_cours: "En cours", termine: "Terminée", echec: "Échec", annule: "Arrêtée" };
  var duree = function (s) {
    if (typeof s !== "number") { return "-"; }
    s = Math.round(s);
    return Math.floor(s / 60) + " min " + (s % 60) + " s";
  };

  // /analyse : filtre texte + case « Tous »
  var filtre = $("filtre-departements");
  if (filtre) {
    $("groupe-filtre").hidden = false;
    var lignes = document.querySelectorAll("[data-departement]");
    filtre.addEventListener("input", function () {
      var q = filtre.value.trim().toLowerCase();
      lignes.forEach(function (l) { l.hidden = q !== "" && l.dataset.departement.toLowerCase().indexOf(q) < 0; });
    });
    $("dep-tous").addEventListener("change", function (e) {
      document.querySelectorAll("input[name=departement]").forEach(function (c) {
        c.checked = false; c.disabled = e.target.checked;
      });
    });
  }

  // compte rendu : filtre par statut
  var statut = $("filtre-statut");
  if (statut) {
    $("groupe-filtre-statut").hidden = false;
    statut.addEventListener("change", function () {
      var rangees = document.querySelectorAll("#table-communes tbody tr[data-statut]");
      var visibles = 0;
      rangees.forEach(function (tr) {
        tr.hidden = statut.value !== "" && tr.dataset.statut !== statut.value;
        if (!tr.hidden) { visibles++; }
      });
      var zone = $("resultat-filtre");
      if (zone) {
        var texte = statut.value === "" ? "" : visibles + " commune(s) affichée(s) sur " + rangees.length + ".";
        if (zone.textContent !== texte) { zone.textContent = texte; }
      }
    });
  }

  // Régions live : le conteneur reste dans le DOM ; on n'écrit que si le texte a changé (pas de ré-annonce toutes les 2 s)
  var annoncer = function (zone, texte, gravite) {
    if (!zone) { return; }
    if (zone.dataset.valeur === undefined) { zone.dataset.valeur = zone.textContent.trim(); }
    if (zone.dataset.valeur === texte) { return; }
    zone.dataset.valeur = texte;
    if (!texte) { zone.replaceChildren(); return; }
    var bloc = document.createElement("div"); bloc.className = "fr-alert fr-alert--" + gravite + " fr-mb-3w";
    var p = document.createElement("p"); p.textContent = texte; bloc.appendChild(p);
    zone.replaceChildren(bloc);
  };

  // /suivi : rafraîchissement toutes les 2 s (sans JavaScript, le lien « Actualiser » reste disponible)
  if ($("suivi")) {
    var maj = function () {
      fetch("/api/etat", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (e) {
        var p = e.progression || {};
        var texte = function (id, v) { $(id).textContent = (v === undefined || v === null) ? "-" : v; };
        texte("etat-statut", ETATS[e.statut] || e.statut);
        texte("n-traites", p.sites_traites); texte("n-total", p.sites_total);
        texte("n-pages", p.pages_visitees); texte("n-liens", p.liens_confirmes);
        $("t-ecoule").textContent = duree(p.ecoule_s); $("t-restant").textContent = duree(p.restant_estime_s);
        var b = $("barre"); b.max = p.sites_total || 1; b.value = p.sites_traites || 0;
        $("btn-arreter").disabled = !(e.actif && e.genre === "campagne");
        annoncer($("etat-erreur"), e.erreur || "", "error");
        annoncer($("etat-message"), e.message || "", "info");
        if (e.campagne_id) {
          var a = document.createElement("a");
          a.href = "/campagnes/" + e.campagne_id; a.textContent = "n° " + e.campagne_id;
          $("etat-campagne").replaceChildren("(analyse ", a, ")");
        }
      }).catch(function () { /* serveur arrêté ou requête échouée : nouvel essai au prochain cycle */ });
    };
    setInterval(maj, 2000);
  }
})();
