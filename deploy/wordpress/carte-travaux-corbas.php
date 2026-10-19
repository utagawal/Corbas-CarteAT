<?php
/**
 * Plugin Name: Carte des travaux de Corbas
 * Description: Intègre la carte des arrêtés de travaux avec le code court [carte_travaux].
 * Version:     1.0.0
 * License:     MIT
 *
 * Installation : copier ce fichier dans wp-content/plugins/ (ou wp-content/mu-plugins/ pour qu'il soit
 * toujours actif), puis l'activer dans Extensions. Utilisation dans une page :
 *     [carte_travaux]
 *     [carte_travaux hauteur="90vh"]
 *     [carte_travaux url="https://corbas.fr/travaux/"]
 * Adresse par défaut modifiable dans wp-config.php :
 *     define('CARTEAT_URL', 'https://maps.utagawavtt.com/ATCorbas/');
 */

if (!defined('ABSPATH')) {
    exit;
}

add_shortcode('carte_travaux', function ($atts) {
    $defaut = defined('CARTEAT_URL') ? CARTEAT_URL : 'https://maps.utagawavtt.com/ATCorbas/';
    $a = shortcode_atts([
        'url'     => $defaut,
        'hauteur' => '80vh',
        'titre'   => 'Carte des travaux et événements sur la voie publique à Corbas',
    ], $atts, 'carte_travaux');

    // Hauteur : uniquement une valeur CSS simple (ex. 80vh, 700px).
    $hauteur = preg_match('/^\d{2,4}(px|vh)$/', $a['hauteur']) ? $a['hauteur'] : '80vh';
    $src = add_query_arg('embed', '1', trailingslashit(esc_url_raw($a['url'])));
    $id = 'carteat-' . wp_unique_id();

    ob_start();
    ?>
    <iframe id="<?php echo esc_attr($id); ?>"
            src="<?php echo esc_url($src); ?>"
            title="<?php echo esc_attr($a['titre']); ?>"
            style="display:block;width:100%;height:<?php echo esc_attr($hauteur); ?>;min-height:520px;border:0"
            loading="lazy" allow="geolocation" referrerpolicy="strict-origin-when-cross-origin"></iframe>
    <p style="font-size:.85em;margin-top:.5em">
        <a href="<?php echo esc_url(trailingslashit($a['url'])); ?>" target="_blank" rel="noopener">Ouvrir la carte en plein écran</a>
    </p>
    <script>
    // Lien direct vers un arrêté (page.../#at=identifiant) : transmis à la carte.
    (function () {
        var h = window.location.hash;
        if (/^#at=[A-Za-z0-9_-]+$/.test(h)) {
            var f = document.getElementById(<?php echo wp_json_encode($id); ?>);
            if (f) f.src = f.src.split('#')[0] + h;
        }
    })();
    </script>
    <?php
    return ob_get_clean();
});
